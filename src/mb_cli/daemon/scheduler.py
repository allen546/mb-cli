"""Deadline countdown scheduler and milestone evaluator."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from ..client import parse_due_date
from .events import MBEvent, ReminderThreshold, DEFAULT_REMINDER_THRESHOLDS
from .state import DaemonStateManager

log = logging.getLogger(__name__)


class DDLScheduler:
    """Evaluates task deadlines against countdown reminder thresholds."""

    def __init__(
        self,
        state_manager: DaemonStateManager,
        reminders: list[ReminderThreshold] | None = None,
    ):
        self.state_manager = state_manager
        self.reminders = sorted(
            reminders or list(DEFAULT_REMINDER_THRESHOLDS),
            key=lambda r: r.threshold_minutes,
            reverse=True,
        )

    def evaluate_deadlines(self, now: datetime | None = None) -> list[MBEvent]:
        """Evaluate all tracked tasks in state against reminder thresholds."""
        current_time = now or datetime.now().astimezone()
        events: list[MBEvent] = []

        for task_id, task in list(self.state_manager.tasks_cache.items()):
            due_str = task.get("due_date")
            if not due_str:
                continue

            due_dt = parse_due_date(due_str)
            if due_dt is None:
                continue

            # Ensure due_dt is timezone-aware for comparison if current_time is
            if due_dt.tzinfo is None and current_time.tzinfo is not None:
                due_dt = due_dt.replace(tzinfo=current_time.tzinfo)
            elif due_dt.tzinfo is not None and current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=due_dt.tzinfo)

            minutes_left = (due_dt - current_time).total_seconds() / 60.0

            # If deadline has passed or task is already submitted, skip reminders
            if minutes_left <= 0:
                continue

            status = str(task.get("status", "not-submitted")).lower()
            if status == "submitted":
                continue

            for reminder in self.reminders:
                if minutes_left <= reminder.threshold_minutes:
                    if not self.state_manager.is_reminder_dispatched(
                        task_id, reminder.name
                    ):
                        self.state_manager.mark_reminder_dispatched(
                            task_id, reminder.name
                        )
                        event = MBEvent(
                            event="deadline_approaching",
                            event_id=f"evt_{task_id}_reminder_{reminder.name}",
                            timestamp=current_time.isoformat(),
                            data={
                                "task_id": task_id,
                                "title": task.get("title", ""),
                                "class_name": task.get("class_name", ""),
                                "class_id": task.get("class_id"),
                                "due_date": due_str,
                                "due_iso": due_dt.isoformat(),
                                "time_remaining_minutes": round(minutes_left, 1),
                                "reminder_threshold": reminder.name,
                                "status": status,
                                "has_submit_button": task.get(
                                    "has_submit_button", False
                                ),
                                "url": task.get("url", ""),
                            },
                        )
                        events.append(event)
                        log.info(
                            "Dispatched %s deadline reminder for task %s (%s)",
                            reminder.name,
                            task_id,
                            task.get("title"),
                        )

        return events
