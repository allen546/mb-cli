"""Persistent state management for the daemon."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from ..client import parse_due_date

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path.home() / ".config" / "mb-crawler" / "daemon_state.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


class DaemonStateManager:
    """Manages persistent state for notification deduplication and deadline tracking."""

    def __init__(self, state_path: str | Path | None = None):
        self.path = (
            Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
        )
        self.last_synced_at: str | None = None
        self.processed_notification_ids: set[int] = set()
        self.dispatched_reminders: set[str] = set()
        self.tasks_cache: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Load state from disk if present."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_synced_at = data.get("last_synced_at")
            self.processed_notification_ids = set(
                data.get("processed_notification_ids", [])
            )
            self.dispatched_reminders = set(
                data.get("dispatched_reminders", [])
            )
            self.tasks_cache = data.get("tasks_cache", {})
        except Exception as exc:
            log.warning("Failed to load daemon state from %s: %s", self.path, exc)

    def save(self) -> None:
        """Persist state atomically to disk."""
        _ensure_parent(self.path)
        data = {
            "last_synced_at": self.last_synced_at,
            "processed_notification_ids": sorted(list(self.processed_notification_ids)),
            "dispatched_reminders": sorted(list(self.dispatched_reminders)),
            "tasks_cache": self.tasks_cache,
        }
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix="daemon_state_",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as tf:
                json.dump(data, tf, indent=2, ensure_ascii=False)
                tf.write("\n")
                tmp_path = Path(tf.name)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            tmp_path.replace(self.path)
        except Exception as exc:
            log.error("Failed to save daemon state to %s: %s", self.path, exc)
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def is_notification_processed(self, notification_id: int) -> bool:
        return notification_id in self.processed_notification_ids

    def mark_notification_processed(self, notification_id: int) -> None:
        self.processed_notification_ids.add(notification_id)
        # Keep set bounded (max 5000 IDs)
        if len(self.processed_notification_ids) > 5000:
            excess = len(self.processed_notification_ids) - 5000
            for item in sorted(list(self.processed_notification_ids))[:excess]:
                self.processed_notification_ids.remove(item)

    def is_reminder_dispatched(self, task_id: str | int, reminder_name: str) -> bool:
        key = f"task_{task_id}:ddl_{reminder_name}"
        return key in self.dispatched_reminders

    def mark_reminder_dispatched(self, task_id: str | int, reminder_name: str) -> None:
        key = f"task_{task_id}:ddl_{reminder_name}"
        self.dispatched_reminders.add(key)
        if len(self.dispatched_reminders) > 5000:
            excess = len(self.dispatched_reminders) - 5000
            for item in sorted(list(self.dispatched_reminders))[:excess]:
                self.dispatched_reminders.remove(item)

    def get_task(self, task_id: str | int) -> dict[str, Any] | None:
        return self.tasks_cache.get(str(task_id))

    def update_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or task.get("task_id") or "")
        if task_id:
            self.tasks_cache[task_id] = task

    def remove_task(self, task_id: str | int) -> None:
        self.tasks_cache.pop(str(task_id), None)

    def prune_old_tasks(self, max_age_days: int = 14) -> int:
        """Prune tasks from tasks_cache whose deadlines passed more than max_age_days ago."""
        now = datetime.now(timezone.utc)
        to_delete = []
        for tid, task in list(self.tasks_cache.items()):
            due_str = task.get("due_date")
            if not due_str:
                continue
            due_dt = parse_due_date(due_str)
            if due_dt:
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                if (now - due_dt).total_seconds() > max_age_days * 86400:
                    to_delete.append(tid)
        for tid in to_delete:
            self.tasks_cache.pop(tid, None)
        return len(to_delete)
