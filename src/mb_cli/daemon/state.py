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

DEFAULT_STATE_PATH = config_dir() / "daemon_state.json"

# Hard cap on tasks_cache entries. Beyond this the daemon's own state file costs
# more to write than the reminders it buys.
MAX_TASKS_CACHE_ENTRIES = 500


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def _sorted_ids(ids: set[int | str] | list[int | str]) -> list[int | str]:
    """Sort notification ids that may mix ints with synthetic string keys.

    ``sorted()`` on a mixed set raises TypeError, which would lose the whole
    save — so numeric ids come first, each group ordered by string form.
    """
    return sorted(ids, key=lambda value: (isinstance(value, str), str(value)))


class DaemonStateManager:
    """Manages persistent state for notification deduplication and deadline tracking."""

    def __init__(self, state_path: str | Path | None = None):
        self.path = (
            Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
        )
        self.last_synced_at: str | None = None
        self.processed_notification_ids: set[int | str] = set()
        self.dispatched_reminders: dict[str, float] = {}
        self.tasks_cache: dict[str, dict[str, Any]] = {}
        # When each task entered the cache, kept beside the cache rather than
        # inside it so bookkeeping keys never leak into a webhook payload.
        self.task_cached_at: dict[str, float] = {}
        # Set by every mutation. The loop used to rewrite this whole file every
        # poll cycle; on the project's target hardware (a Pi SD card) that is
        # tens of KB of needless writes.
        self._dirty = False
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
            raw_cached_at = data.get("task_cached_at", {})
            if isinstance(raw_cached_at, dict):
                self.task_cached_at = {
                    str(k): float(v)
                    for k, v in raw_cached_at.items()
                    if isinstance(v, (int, float))
                }
            self._migrate_inline_cached_at()
            self._dirty = False
        except Exception as exc:
            log.warning("Failed to load daemon state from %s: %s", self.path, exc)

    def _migrate_inline_cached_at(self) -> None:
        """Adopt timestamps an older version stored inside the task dicts."""
        for tid, task in list(self.tasks_cache.items()):
            if not isinstance(task, dict):
                continue
            inline = task.pop("_cached_at", None)
            if inline is None:
                continue
            if tid in self.task_cached_at:
                continue
            try:
                self.task_cached_at[tid] = float(inline)
            except (TypeError, ValueError):
                try:
                    self.task_cached_at[tid] = datetime.fromisoformat(
                        str(inline)
                    ).timestamp()
                except (TypeError, ValueError):
                    continue

    def save(self, force: bool = False) -> None:
        """Persist state atomically to disk.

        Skipped when nothing has changed since the last write. A missing file
        always warrants a write, even with no pending mutations.
        """
        if not force and not self._dirty and self.path.exists():
            return
        # Keep the cache bounded even between the 12-hourly syncs, which are the
        # only other place pruning happens.
        if len(self.tasks_cache) > MAX_TASKS_CACHE_ENTRIES:
            self.bound_tasks_cache(MAX_TASKS_CACHE_ENTRIES)
        _ensure_parent(self.path)
        data = {
            "last_synced_at": self.last_synced_at,
            # Mixed int and synthetic-string ids cannot be sorted together, so
            # order by type first. A plain sorted() raises TypeError and the
            # whole save is lost.
            "processed_notification_ids": _sorted_ids(
                self.processed_notification_ids
            ),
            "dispatched_reminders": dict(self.dispatched_reminders),
            "tasks_cache": self.tasks_cache,
            "task_cached_at": dict(self.task_cached_at),
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
            self._dirty = False
        except Exception as exc:
            log.error("Failed to save daemon state to %s: %s", self.path, exc)
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def is_notification_processed(self, notification_id: int | str) -> bool:
        return notification_id in self.processed_notification_ids

    def mark_notification_processed(self, notification_id: int | str) -> None:
        self.processed_notification_ids.add(notification_id)
        self._dirty = True
        # Keep set bounded (max 5000 IDs)
        if len(self.processed_notification_ids) > 5000:
            excess = len(self.processed_notification_ids) - 5000
            for item in sorted(
                self.processed_notification_ids,
                key=lambda value: (isinstance(value, str), str(value)),
            )[:excess]:
                self.processed_notification_ids.discard(item)

    def is_reminder_dispatched(self, task_id: str | int, reminder_name: str) -> bool:
        key = f"task_{task_id}:ddl_{reminder_name}"
        return key in self.dispatched_reminders

    def mark_reminder_dispatched(self, task_id: str | int, reminder_name: str) -> None:
        key = f"task_{task_id}:ddl_{reminder_name}"
        # dict preserves insertion order, so eviction can drop the OLDEST
        # entries.  Sorting the composite string keys would order
        # "task_10:…" before "task_9:…", evicting the newest instead.
        self.dispatched_reminders[key] = time.time()
        self._dirty = True
        if len(self.dispatched_reminders) > 5000:
            excess = len(self.dispatched_reminders) - 5000
            for item in list(self.dispatched_reminders.keys())[:excess]:
                self.dispatched_reminders.pop(item, None)

    def get_task(self, task_id: str | int) -> dict[str, Any] | None:
        return self.tasks_cache.get(str(task_id))

    def _cached_at_of(self, task_id: str, task: dict[str, Any]) -> float:
        """When ``task`` entered the cache, as a unix timestamp (0 if unknown)."""
        stamp = self.task_cached_at.get(task_id)
        if stamp is None:
            # Pre-migration state files kept the stamp inside the task dict.
            inline = task.get("_cached_at") if isinstance(task, dict) else None
            if inline is not None:
                try:
                    stamp = float(inline)
                except (TypeError, ValueError):
                    try:
                        stamp = datetime.fromisoformat(str(inline)).timestamp()
                    except (TypeError, ValueError):
                        stamp = None
        return float(stamp) if stamp is not None else 0.0

    def update_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or task.get("task_id") or "")
        if task_id:
            self.tasks_cache[task_id] = task
            if task_id not in self.task_cached_at:
                self.task_cached_at[task_id] = time.time()
            self._dirty = True

    def remove_task(self, task_id: str | int) -> None:
        key = str(task_id)
        self.tasks_cache.pop(key, None)
        self.task_cached_at.pop(key, None)
        self._dirty = True

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
            due_str = task.get("due_date") if isinstance(task, dict) else None
            due_dt = parse_due_date(due_str) if due_str else None
            if due_dt is None:
                # Fall back to when we cached it so undated tasks still age out.
                cached_at = self._cached_at_of(tid, task)
                if cached_at <= 0.0:
                    self.task_cached_at[tid] = time.time()
                    self._dirty = True
                    continue
                if now.timestamp() - cached_at > cutoff:
                    to_delete.append(tid)
                continue
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
            if (now - due_dt).total_seconds() > cutoff:
                to_delete.append(tid)
        for tid in to_delete:
            self.tasks_cache.pop(tid, None)
            self.task_cached_at.pop(tid, None)
        if to_delete:
            self._dirty = True
        return len(to_delete)

    def bound_tasks_cache(self, max_entries: int = 500) -> int:
        """Hard-cap tasks_cache size, dropping the oldest-cached entries first.

        ``prune_old_tasks`` only runs during the 12-hourly sync, so this is what
        keeps the cache bounded in between. It is called from :meth:`save`, so
        the cap holds whatever path mutates the cache.
        """
        if len(self.tasks_cache) <= max_entries:
            return 0
        ordered = sorted(
            self.tasks_cache.items(),
            # Oldest first. Ties (no recorded timestamp) fall back to insertion
            # order, which `sorted` preserves.
            key=lambda item: self._cached_at_of(item[0], item[1]),
        )
        excess = len(ordered) - max_entries
        for tid, _ in ordered[:excess]:
            self.tasks_cache.pop(tid, None)
            self.task_cached_at.pop(tid, None)
        if excess:
            self._dirty = True
        return excess
