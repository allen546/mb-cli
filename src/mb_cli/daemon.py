"""Daemon support for mb-cli — checks ManageBac during active windows and
delivers alerts only when something actually changed.

Each check is lightweight: one index page + notifications hub.  Detail pages
are fetched *only* for tasks that appear new or changed in the index diff.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
import json
import logging
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import time
from zoneinfo import ZoneInfo

import requests

from .client import ManageBacClient
from .config import AppState, save_profile

log = logging.getLogger(__name__)

DEFAULT_DAEMON_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.json"
DEFAULT_WEBHOOK_URL = "http://127.0.0.1:42617/webhook"
DEFAULT_PID_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.pid"
DEFAULT_LOG_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.log"
DEFAULT_SNAPSHOT_PATH = Path.home() / ".config" / "mb-crawler" / "snapshot.json"

DEFAULT_ACTIVE_WINDOWS: list[list[str]] = [
    ["07:00", "07:30"],
    ["11:30", "13:30"],
    ["17:30", "23:00"],
]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


# ── Config ──────────────────────────────────────────────────────────────


def load_daemon_config(path: str | None = None) -> dict:
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    if not daemon_path.exists():
        return {
            "delivery": {
                "mode": "webhook",
                "webhook_url": DEFAULT_WEBHOOK_URL,
            },
            "active_windows": DEFAULT_ACTIVE_WINDOWS,
            "verify_tls": True,
            "snapshot_file": str(DEFAULT_SNAPSHOT_PATH),
            "pid_file": str(DEFAULT_PID_PATH),
            "log_file": str(DEFAULT_LOG_PATH),
        }
    data = json.loads(daemon_path.read_text(encoding="utf-8"))
    if "delivery" not in data:
        data["delivery"] = {
            "mode": "webhook",
            "webhook_url": data.pop("webhook_url", DEFAULT_WEBHOOK_URL),
        }
    # Migrate legacy active_hours_start/end → active_windows
    if "active_windows" not in data:
        start = data.pop("active_hours_start", 7)
        end = data.pop("active_hours_end", 23)
        data["active_windows"] = [[f"{start:02d}:00", f"{end:02d}:00"]]
    return data


def save_daemon_config(data: dict, path: str | None = None) -> Path:
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    _ensure_parent(daemon_path)
    daemon_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.chmod(daemon_path, 0o600)
    return daemon_path


# ── Snapshot ────────────────────────────────────────────────────────────


def _load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_snapshot(path: Path, data: dict) -> None:
    _ensure_parent(path)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.chmod(path, 0o600)


# ── Index diffing ───────────────────────────────────────────────────────


def _task_index(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks if t.get("id")}


def diff_index(old: dict, new: dict) -> tuple[list[dict], list[dict]]:
    """Diff two ``crawl_index()`` results.

    Returns ``(alerts, changed_ids)`` — alerts to deliver and the IDs of
    tasks whose index data changed (for optional detail fetching).
    """
    alerts: list[dict] = []
    changed_ids: list[str] = []

    old_upcoming = _task_index(old.get("upcoming", []))
    new_upcoming = _task_index(new.get("upcoming", []))

    # New tasks
    for tid, task in new_upcoming.items():
        if tid not in old_upcoming:
            alerts.append(
                {
                    "type": "new_upcoming",
                    "severity": "medium",
                    "task": task,
                    "message": f"New task: {task['title']} due {task.get('due_date', '?')} ({task.get('class_name', '')})",
                }
            )
            changed_ids.append(tid)

    # Grade changes
    for tid, task in new_upcoming.items():
        old_task = old_upcoming.get(tid)
        if not old_task:
            continue
        if task.get("grade_letter") and task.get("grade_letter") != old_task.get(
            "grade_letter"
        ):
            alerts.append(
                {
                    "type": "new_grade",
                    "severity": "info",
                    "task": task,
                    "message": f"Grade posted: {task['title']} -> {task.get('grade_letter')} {task.get('grade_score', '')}",
                }
            )
            changed_ids.append(tid)

    # New overdue
    for tid, task in new_upcoming.items():
        if task.get("view") == "overdue" or (
            old_upcoming.get(tid) and old_upcoming[tid].get("view") != "overdue"
        ):
            if task.get("view") == "overdue" and tid not in _task_index(
                old.get("upcoming", [])
            ):
                alerts.append(
                    {
                        "type": "new_overdue",
                        "severity": "high",
                        "task": task,
                        "message": f"Task is now overdue: {task['title']} ({task.get('class_name', '')})",
                    }
                )
                changed_ids.append(tid)

    # Notification changes
    old_unread = old.get("notifications", {}).get("unread_count", 0)
    new_unread = new.get("notifications", {}).get("unread_count", 0)
    if new_unread > old_unread:
        delta = new_unread - old_unread
        alerts.append(
            {
                "type": "new_notifications",
                "severity": "medium",
                "task": {},
                "message": f"{delta} new notification(s) ({new_unread} unread total)",
            }
        )

    return alerts, changed_ids


# Keep the full diff for backward compat (MCP server, --once with crawl_all)
def _diff_snapshots_full(old: dict, new: dict) -> list[dict]:
    alerts = []
    old_overdue = _task_index(old.get("overdue", []))
    new_overdue = _task_index(new.get("overdue", []))
    old_upcoming = _task_index(old.get("upcoming", []))
    new_upcoming = _task_index(new.get("upcoming", []))
    all_old = {**old_overdue, **old_upcoming, **_task_index(old.get("past", []))}
    all_new = {**new_overdue, **new_upcoming, **_task_index(new.get("past", []))}

    for tid, task in new_overdue.items():
        if tid not in old_overdue:
            alerts.append(
                {
                    "type": "new_overdue",
                    "severity": "high",
                    "task": task,
                    "message": f"Task is now overdue: {task['title']} ({task.get('class_name', '')})",
                }
            )
    for tid, task in new_upcoming.items():
        if tid not in old_upcoming:
            alerts.append(
                {
                    "type": "new_upcoming",
                    "severity": "medium",
                    "task": task,
                    "message": f"New upcoming task: {task['title']} due {task.get('due_date', '?')} ({task.get('class_name', '')})",
                }
            )
    for tid, task in all_new.items():
        old_task = all_old.get(tid)
        if not old_task:
            continue
        if task.get("grade_letter") and not old_task.get("grade_letter"):
            alerts.append(
                {
                    "type": "new_grade",
                    "severity": "info",
                    "task": task,
                    "message": f"Grade posted: {task['title']} -> {task.get('grade_letter')} {task.get('grade_score', '')}",
                }
            )
    return alerts


# ── Active window scheduling ────────────────────────────────────────────


def _parse_window(w: list[str]) -> tuple[dt_time, dt_time]:
    """Parse ``["HH:MM", "HH:MM"]`` into ``(start_time, end_time)``."""
    parts_s = w[0].split(":")
    parts_e = w[1].split(":")
    return (
        dt_time(int(parts_s[0]), int(parts_s[1])),
        dt_time(int(parts_e[0]), int(parts_e[1])),
    )


def _now_local() -> datetime:
    return datetime.now(ZoneInfo("UTC")).astimezone()


def _is_in_window(now: dt_time, start: dt_time, end: dt_time) -> bool:
    if start <= end:
        return start <= now <= end
    # wraps midnight (shouldn't happen with our windows, but be safe)
    return now >= start or now <= end


def _next_active_window(daemon_config: dict) -> datetime:
    """Return the datetime of the next active window start.

    If we're currently *in* a window, returns now (check immediately).
    """
    windows = daemon_config.get("active_windows", DEFAULT_ACTIVE_WINDOWS)
    now = _now_local()
    now_t = now.time()

    # Check if we're inside any window right now
    for w in windows:
        start, end = _parse_window(w)
        if _is_in_window(now_t, start, end):
            return now

    # Find the next window today or tomorrow
    for w in windows:
        start, _ = _parse_window(w)
        candidate = now.replace(
            hour=start.hour, minute=start.minute, second=0, microsecond=0
        )
        if candidate > now:
            return candidate

    # All windows passed today → first window tomorrow
    first_start, _ = _parse_window(windows[0])
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=first_start.hour, minute=first_start.minute, second=0, microsecond=0
    )


def _time_until(target: datetime) -> float:
    """Seconds from now until *target* (minimum 1s)."""
    delta = (target - _now_local()).total_seconds()
    return max(delta, 1.0)


# ── Delivery ────────────────────────────────────────────────────────────


def _log(path: Path, message: str) -> None:
    _ensure_parent(path)
    line = f"[{datetime.now().isoformat()}] {message}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _post_webhook(
    webhook_url: str, alerts: list[dict], result: dict, verify: bool = True
) -> bool:
    message = "\n".join(alert["message"] for alert in alerts)
    footer = (
        f"\n[mb-crawler daemon] student={result.get('student_name')} "
        f"upcoming={result.get('summary', {}).get('upcoming_count', '?')}"
    )
    payload = {"message": message + footer}
    response = requests.post(webhook_url, json=payload, timeout=60, verify=verify)
    return response.status_code < 400


def _send_channel(
    channel_id: str,
    recipient: str,
    alerts: list[dict],
    result: dict,
    zeroclaw_bin: str | None = None,
) -> bool:
    """Deliver alerts via ``zeroclaw channel send`` — no LLM call."""
    message = "\n".join(alert["message"] for alert in alerts)
    footer = (
        f"\n[mb-cli] "
        f"upcoming={result.get('summary', {}).get('upcoming_count', '?')}"
    )
    text = message + footer

    binary = zeroclaw_bin or shutil.which("zeroclaw") or "zeroclaw"
    cmd = [
        binary,
        "channel",
        "send",
        text,
        "--channel-id",
        channel_id,
        "--recipient",
        recipient,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning(
                "channel send failed (rc=%d): %s", proc.returncode, proc.stderr.strip()
            )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.error("channel send error: %s", exc)
        return False


def _deliver_alerts(
    delivery: dict, alerts: list[dict], result: dict, verify_tls: bool = True
) -> bool:
    """Dispatch alerts to the configured delivery backend."""
    mode = delivery.get("mode", "webhook")
    if mode == "channel_send":
        return _send_channel(
            channel_id=delivery["channel_id"],
            recipient=delivery["recipient"],
            alerts=alerts,
            result=result,
            zeroclaw_bin=delivery.get("zeroclaw_bin"),
        )
    return _post_webhook(
        webhook_url=delivery.get("webhook_url", DEFAULT_WEBHOOK_URL),
        alerts=alerts,
        result=result,
        verify=verify_tls,
    )


# ── Check cycle ─────────────────────────────────────────────────────────


def run_daemon_check(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
) -> dict:
    """One lightweight check cycle: index + notifications, diff, selective detail."""
    snapshot_path = Path(daemon_config["snapshot_file"]).expanduser()
    old = _load_snapshot(snapshot_path)

    # 1. Lightweight index check (2 HTTP requests)
    index = client.crawl_index()

    # 2. Diff against snapshot
    alerts, changed_ids = diff_index(old, index)

    # 3. Fetch detail ONLY for changed tasks
    for tid in changed_ids:
        task = next(
            (t for t in index.get("upcoming", []) if t.get("id") == tid), None
        )
        if task and task.get("link"):
            detail = client.get_task_detail(task["link"])
            if detail:
                task["detail"] = detail
            time.sleep(random.uniform(1.0, 3.0))

    # 4. Save updated snapshot
    index["detail_fetches"] = len(changed_ids)
    _save_snapshot(snapshot_path, index)

    # 5. Deliver
    delivered = False
    if alerts and not dry_run:
        delivery = daemon_config.get("delivery", {"mode": "webhook"})
        verify = daemon_config.get("verify_tls", True)
        delivered = _deliver_alerts(delivery, alerts, index, verify_tls=verify)

    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "detail_fetches": len(changed_ids),
        "delivered": delivered,
        "snapshot_file": str(snapshot_path),
    }


def run_daemon_once(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
) -> dict:
    """Legacy full-crawl mode (used by ``mb daemon start --once``)."""
    snapshot_path = Path(daemon_config["snapshot_file"]).expanduser()
    old = _load_snapshot(snapshot_path)
    result = client.crawl_all(max_pages=10, fetch_details=False)
    alerts = _diff_snapshots_full(old, result)
    _save_snapshot(snapshot_path, result)

    delivered = False
    if alerts and not dry_run:
        delivery = daemon_config.get("delivery", {"mode": "webhook"})
        verify = daemon_config.get("verify_tls", True)
        delivered = _deliver_alerts(delivery, alerts, result, verify_tls=verify)
    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "delivered": delivered,
        "snapshot_file": str(snapshot_path),
    }


# ── Configure helpers ───────────────────────────────────────────────────


def configure_webhook(url: str, path: str | None = None) -> dict:
    config = load_daemon_config(path)
    config["delivery"] = {"mode": "webhook", "webhook_url": url}
    save_daemon_config(config, path)
    return config


def configure_channel_send(
    channel_id: str,
    recipient: str,
    path: str | None = None,
    zeroclaw_bin: str | None = None,
) -> dict:
    config = load_daemon_config(path)
    delivery: dict[str, str] = {
        "mode": "channel_send",
        "channel_id": channel_id,
        "recipient": recipient,
    }
    if zeroclaw_bin:
        delivery["zeroclaw_bin"] = zeroclaw_bin
    config["delivery"] = delivery
    save_daemon_config(config, path)
    return config


# ── Main loop ───────────────────────────────────────────────────────────


def start_loop(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
    once: bool = False,
) -> dict:
    pid_path = Path(daemon_config["pid_file"]).expanduser()
    log_path = Path(daemon_config["log_file"]).expanduser()
    _ensure_parent(pid_path)
    pid_path.write_text(
        str(Path("/dev/null")) if once else str(os.getpid()), encoding="utf-8"
    )

    backoff = 0.0  # seconds of additional delay after no-change checks
    max_backoff = 3600.0  # cap at 1 hour

    try:
        while True:
            next_window = _next_active_window(daemon_config)
            wait = _time_until(next_window)
            in_window = wait < 1.0

            if not in_window:
                _log(log_path, f"sleeping {wait:.0f}s until next window")
                time.sleep(min(wait, 600))
                continue

            # We're in an active window — do a check
            result = run_daemon_check(client, daemon_config, dry_run=dry_run)
            _log(
                log_path,
                f"check alert_count={result['alert_count']} "
                f"details_fetched={result['detail_fetches']} "
                f"delivered={result['delivered']}",
            )

            if once:
                return result

            # Backoff: if nothing changed, wait longer next time within the window
            if result["alert_count"] == 0:
                backoff = min(backoff + 600, max_backoff)
            else:
                backoff = 0.0

            # Sleep until next window or backoff, whichever is sooner
            next_check = _next_active_window(daemon_config)
            wait = _time_until(next_check) + backoff
            time.sleep(min(wait, 600))
    finally:
        if pid_path.exists():
            pid_path.unlink()


# ── Stop ────────────────────────────────────────────────────────────────


def _is_mb_cli_pid(pid: int) -> bool:
    """Check whether *pid* belongs to an mb-cli process."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        cmdline = result.stdout.strip()
        return "mb-cli" in cmdline or "mb_cli" in cmdline or "mb_crawler" in cmdline
    except (subprocess.TimeoutExpired, OSError):
        return False


def stop_daemon(path: str | None = None) -> dict:
    config = load_daemon_config(path)
    pid_path = Path(config["pid_file"]).expanduser()
    if not pid_path.exists():
        return {
            "stopped": False,
            "reason": "pid_file_missing",
            "pid_file": str(pid_path),
        }

    raw = pid_path.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw)
    except ValueError:
        pid_path.unlink()
        return {"stopped": False, "reason": "invalid_pid", "pid_file": str(pid_path)}

    if pid <= 0:
        pid_path.unlink(missing_ok=True)
        return {"stopped": False, "reason": "invalid_pid", "pid_file": str(pid_path)}

    if not _is_mb_cli_pid(pid):
        pid_path.unlink(missing_ok=True)
        return {
            "stopped": False,
            "reason": "not_mb_cli_process",
            "pid": pid,
            "pid_file": str(pid_path),
        }

    os.kill(pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid, "pid_file": str(pid_path)}
