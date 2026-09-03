"""Deadline countdown scheduler and milestone evaluator."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from collections.abc import Callable
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
        submission_checker: Callable[[str, str], bool] | None = None,
    ):
        self.state_manager = state_manager
        self.submission_checker = submission_checker
        self.reminders = sorted(
            reminders or list(DEFAULT_REMINDER_THRESHOLDS),
            key=lambda r: r.threshold_minutes,
            reverse=True,
        )

    def evaluate_deadlines(
        self, now: datetime | None = None, auto_mark: bool = True
    ) -> list[MBEvent]:
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

            # Ensure due_dt and task_now have matching tzinfo without mutating current_time
            task_now = current_time
            if due_dt.tzinfo is None and task_now.tzinfo is not None:
                due_dt = due_dt.replace(tzinfo=task_now.tzinfo)
            elif due_dt.tzinfo is not None and task_now.tzinfo is None:
                task_now = task_now.replace(tzinfo=due_dt.tzinfo)

            minutes_left = (due_dt - task_now).total_seconds() / 60.0

            # If deadline has passed or task is already submitted, skip reminders
            if minutes_left <= 0:
                continue

            status = str(task.get("status", "not-submitted")).lower()
            if status == "submitted":
                continue

            checked_live = False
            for reminder in self.reminders:
                if minutes_left <= reminder.threshold_minutes:
                    if not self.state_manager.is_reminder_dispatched(
                        task_id, reminder.name
                    ):
                        # Live-verify on ManageBac if student submitted in the meantime
                        if not checked_live and self.submission_checker:
                            checked_live = True
                            c_id = task.get("class_id")
                            if c_id:
                                try:
                                    if self.submission_checker(str(c_id), str(task_id)):
                                        task["status"] = "submitted"
                                        self.state_manager.update_task(task)
                                        log.info("Task %s live-verified as submitted — suppressing reminders", task_id)
                                        break
                                except Exception as exc:
                                    log.debug("Live submission check error for task %s: %s", task_id, exc)
                        if auto_mark:
                            self.state_manager.mark_reminder_dispatched(
                                task_id, reminder.name
                            )
                        t_id = int(task_id) if str(task_id).isdigit() else task_id
                        raw_c_id = task.get("class_id")
                        c_id = int(raw_c_id) if raw_c_id is not None and str(raw_c_id).isdigit() else raw_c_id

                        event = MBEvent(
                            event="deadline_approaching",
                            event_id=f"evt_{task_id}_reminder_{reminder.name}",
                            timestamp=task_now.isoformat(),
                            data={
                                "task_id": t_id,
                                "title": task.get("title", ""),
                                "class_name": task.get("class_name", ""),
                                "class_id": c_id,
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
