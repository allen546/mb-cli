"""Webhook daemon support for mb-crawler."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import time

import requests

from .client import ManageBacClient
from .config import AppState, save_profile

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
            "webhook_url": DEFAULT_WEBHOOK_URL,
            "interval": 900,
            "verify_tls": True,
            "snapshot_file": str(DEFAULT_SNAPSHOT_PATH),
            "pid_file": str(DEFAULT_PID_PATH),
            "log_file": str(DEFAULT_LOG_PATH),
        }
    return json.loads(daemon_path.read_text(encoding="utf-8"))


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
        verify = daemon_config.get("verify_tls", True)
        delivered = _post_webhook(
            daemon_config["webhook_url"], alerts, result, verify=verify
        )
    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "delivered": delivered,
        "snapshot_file": str(snapshot_path),
    }


def configure_webhook(url: str, path: str | None = None) -> dict:
    config = load_daemon_config(path)
    config["webhook_url"] = url
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


def _is_mb_crawler_pid(pid: int) -> bool:
    """Check whether *pid* belongs to an mb-crawler process."""
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
        return "mb-crawler" in cmdline or "mb_crawler" in cmdline
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

    if not _is_mb_crawler_pid(pid):
        pid_path.unlink(missing_ok=True)
        return {
            "stopped": False,
            "reason": "not_mb_crawler_process",
            "pid": pid,
            "pid_file": str(pid_path),
        }

    os.kill(pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid, "pid_file": str(pid_path)}
