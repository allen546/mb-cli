"""Daemon support for mb-cli — polls ManageBac and delivers alerts.

Delivery modes:
  - ``webhook``: POST alerts to an HTTP endpoint (legacy).
  - ``channel_send``: push alerts via ``zeroclaw channel send`` (no LLM call).
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import requests

from .client import ManageBacClient
from .config import AppState, save_profile

log = logging.getLogger(__name__)

DEFAULT_DAEMON_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.json"
DEFAULT_WEBHOOK_URL = "http://127.0.0.1:42617/webhook"
DEFAULT_PID_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.pid"
DEFAULT_LOG_PATH = Path.home() / ".config" / "mb-crawler" / "daemon.log"
DEFAULT_SNAPSHOT_PATH = Path.home() / ".config" / "mb-crawler" / "snapshot.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def load_daemon_config(path: str | None = None) -> dict:
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    if not daemon_path.exists():
        return {
            "delivery": {
                "mode": "webhook",
                "webhook_url": DEFAULT_WEBHOOK_URL,
            },
            "interval": 900,
            "verify_tls": True,
            "snapshot_file": str(DEFAULT_SNAPSHOT_PATH),
            "pid_file": str(DEFAULT_PID_PATH),
            "log_file": str(DEFAULT_LOG_PATH),
        }
    data = json.loads(daemon_path.read_text(encoding="utf-8"))
    # Migrate legacy flat config → nested delivery block
    if "delivery" not in data:
        data["delivery"] = {
            "mode": "webhook",
            "webhook_url": data.pop("webhook_url", DEFAULT_WEBHOOK_URL),
        }
    return data


def save_daemon_config(data: dict, path: str | None = None) -> Path:
    daemon_path = Path(path).expanduser() if path else DEFAULT_DAEMON_PATH
    _ensure_parent(daemon_path)
    daemon_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.chmod(daemon_path, 0o600)
    return daemon_path


def _task_index(tasks: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tasks if t.get("id")}


def diff_snapshots(old: dict, new: dict) -> list[dict]:
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
        f"upcoming={result['summary']['upcoming_count']} "
        f"past={result['summary']['past_count']} "
        f"overdue={result['summary']['overdue_count']}"
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
        f"upcoming={result['summary']['upcoming_count']} "
        f"overdue={result['summary']['overdue_count']}"
    )
    text = message + footer

    binary = zeroclaw_bin or shutil.which("zeroclaw") or "zeroclaw"
    cmd = [binary, "channel", "send", text, "--channel-id", channel_id,
           "--recipient", recipient]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning("channel send failed (rc=%d): %s", proc.returncode,
                        proc.stderr.strip())
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
    # Default: webhook
    return _post_webhook(
        webhook_url=delivery.get("webhook_url", DEFAULT_WEBHOOK_URL),
        alerts=alerts,
        result=result,
        verify=verify_tls,
    )


def run_daemon_once(
    client: ManageBacClient,
    daemon_config: dict,
    dry_run: bool = False,
) -> dict:
    snapshot_path = Path(daemon_config["snapshot_file"]).expanduser()
    old = _load_snapshot(snapshot_path)
    result = client.crawl_all(max_pages=10, fetch_details=False)
    alerts = diff_snapshots(old, result)
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


def configure_webhook(url: str, path: str | None = None) -> dict:
    config = load_daemon_config(path)
    config["delivery"] = {"mode": "webhook", "webhook_url": url}
    save_daemon_config(config, path)
    return config


def configure_channel_send(
    channel_id: str, recipient: str, path: str | None = None,
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

    try:
        while True:
            result = run_daemon_once(client, daemon_config, dry_run=dry_run)
            _log(
                log_path,
                f"ran cycle alert_count={result['alert_count']} delivered={result['delivered']}",
            )
            if once:
                return result
            time.sleep(int(daemon_config.get("interval", 900)))
    finally:
        if pid_path.exists():
            pid_path.unlink()


def _is_mb_cli_pid(pid: int) -> bool:
    """Check whether *pid* belongs to an mb-cli process."""
    import subprocess

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
