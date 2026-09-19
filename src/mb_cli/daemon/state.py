"""Persistent state management for the daemon."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from ..client import parse_due_date
from ..config import config_dir

log = logging.getLogger(__name__)


class _LazyPath:
    """A module-level path attribute that resolves when read, not when imported.

    `config_dir()` re-reads `$HOME` on every call (see its docstring). A plain
    `DEFAULT_STATE_PATH = config_dir() / "..."` captures the value once, at
    import time, so a process whose environment changes afterwards — or a test
    that redirects `HOME` — writes state to one directory while everything else
    reads from another. Subclassing `Path` cannot defer that, so this proxies
    every attribute access to a freshly resolved value.
    """

    _filename = "daemon_state.json"

    def _resolve(self) -> Path:
        return config_dir() / self._filename

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)

    def __fspath__(self) -> str:
        return str(self._resolve())

    def __truediv__(self, other):
        return self._resolve() / other

    def __str__(self) -> str:
        return str(self._resolve())

    def __repr__(self) -> str:
        return repr(self._resolve())

    def __eq__(self, other) -> bool:
        return self._resolve() == other

    def __hash__(self) -> int:
        return hash(self._resolve())


# Resolved on every read; assigning to this name in a test still works, since
# `monkeypatch.setattr` replaces the module attribute outright.
DEFAULT_STATE_PATH = _LazyPath()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


class DaemonStateManager:
    """Manages persistent state for notification deduplication and deadline tracking."""

    def __init__(
        self, state_path: str | Path | None = None, *, persist: bool = True
    ):
        self.path = (
            Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
        )
        # A non-persisting manager still tracks dedup in memory for the length of
        # one run, so a single cycle cannot process the same notification twice —
        # but nothing it records survives the process. `--dry-run` needs exactly
        # that: it must be able to *show* the work it would do without consuming
        # the very notifications the next real run is supposed to deliver.
        self.persist = persist
        self.last_synced_at: str | None = None
        self.processed_notification_ids: set[int] = set()
        self.dispatched_reminders: dict[str, float] = {}
        self.tasks_cache: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Load state from disk if present.

        Reads are never suppressed: a dry run should report the same alerts the
        next real run would, which means deduplicating against what has already
        been delivered rather than replaying the whole history.
        """
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_synced_at = data.get("last_synced_at")
            self.processed_notification_ids = set(
                data.get("processed_notification_ids", [])
            )
            raw_reminders = data.get("dispatched_reminders", [])
            # Tolerate the legacy list format; migrate to insertion-ordered dict.
            if isinstance(raw_reminders, dict):
                self.dispatched_reminders = {
                    str(k): float(v) for k, v in raw_reminders.items()
                }
            else:
                self.dispatched_reminders = {
                    str(k): 0.0 for k in (raw_reminders or [])
                }
            self.tasks_cache = data.get("tasks_cache", {})
        except Exception as exc:
            log.warning("Failed to load daemon state from %s: %s", self.path, exc)

    def save(self) -> None:
        """Persist state atomically to disk.

        A no-op on a non-persisting manager, which is how ``--dry-run`` stays
        side-effect-free: marking notifications processed is what makes them
        invisible to the *next* run, so persisting that during a dry run would
        silently swallow the real deliveries it was only supposed to preview.
        """
        if not self.persist:
            return
        _ensure_parent(self.path)
        data = {
            "last_synced_at": self.last_synced_at,
            "processed_notification_ids": sorted(list(self.processed_notification_ids)),
            "dispatched_reminders": dict(self.dispatched_reminders),
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
        # dict preserves insertion order, so eviction can drop the OLDEST
        # entries.  Sorting the composite string keys would order
        # "task_10:…" before "task_9:…", evicting the newest instead.
        self.dispatched_reminders[key] = time.time()
        if len(self.dispatched_reminders) > 5000:
            excess = len(self.dispatched_reminders) - 5000
            for item in list(self.dispatched_reminders.keys())[:excess]:
                self.dispatched_reminders.pop(item, None)

    def get_task(self, task_id: str | int) -> dict[str, Any] | None:
        return self.tasks_cache.get(str(task_id))

    def update_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or task.get("task_id") or "")
        if task_id:
            self.tasks_cache[task_id] = task

    def remove_task(self, task_id: str | int) -> None:
        self.tasks_cache.pop(str(task_id), None)

    def prune_old_tasks(self, max_age_days: int = 14) -> int:
        """Prune tasks from tasks_cache whose deadlines passed more than max_age_days ago.

        Tasks with no parseable due date are also dropped once they are older
        than the cutoff, otherwise they would accumulate forever (the daemon
        rewrites this whole file on every poll).
        """
        now = datetime.now(timezone.utc)
        cutoff = max_age_days * 86400
        to_delete = []
        for tid, task in list(self.tasks_cache.items()):
            due_str = task.get("due_date")
            due_dt = parse_due_date(due_str) if due_str else None
            if due_dt is None:
                # Fall back to when we cached it so undated tasks still age out.
                cached_at = task.get("_cached_at")
                try:
                    age = (
                        now - datetime.fromisoformat(str(cached_at))
                        if cached_at
                        else None
                    )
                except (TypeError, ValueError):
                    age = None
                if cached_at is None:
                    task["_cached_at"] = now.isoformat()
                elif age is not None and age.total_seconds() > cutoff:
                    to_delete.append(tid)
                continue
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
            if (now - due_dt).total_seconds() > cutoff:
                to_delete.append(tid)
        for tid in to_delete:
            self.tasks_cache.pop(tid, None)
        return len(to_delete)

    def bound_tasks_cache(self, max_entries: int = 500) -> int:
        """Hard-cap tasks_cache size, dropping oldest-cached entries first."""
        if len(self.tasks_cache) <= max_entries:
            return 0
        def _sort_key(item) -> str:
            tid, task = item
            return str(task.get("_cached_at") or "")
        ordered = sorted(self.tasks_cache.items(), key=_sort_key)
        excess = len(ordered) - max_entries
        for tid, _ in ordered[:excess]:
            self.tasks_cache.pop(tid, None)
        return excess
