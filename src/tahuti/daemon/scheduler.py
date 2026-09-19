"""Deadline countdown scheduler and milestone evaluator."""

from __future__ import annotations

from datetime import datetime, tzinfo
import logging
import re
from collections.abc import Callable
from zoneinfo import ZoneInfo

from ..client import parse_due_date
from ..task_status import is_task_submitted_or_graded
from .events import (
    DEFAULT_REMINDER_THRESHOLDS,
    MBEvent,
    ReminderThreshold,
    standardize_task_payload,
)
from .state import DaemonStateManager

log = logging.getLogger(__name__)


def resolve_school_timezone(value: object) -> tzinfo | None:
    """Resolve a configured school timezone, or None when unset/unusable.

    ManageBac renders due dates in the *school's* wall clock
    ("September 15, 2026 at 23:59") and :func:`parse_due_date` hands them back as
    naive datetimes, so somebody has to say which clock that was. Without this
    the scheduler silently assumes it was the daemon host's — on a Pi in UTC
    serving a UTC+8 school, a 23:59 deadline reads as 23:59 UTC and every
    reminder fires about 16 hours late.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, tzinfo):
        return value
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError / bad key
        log.warning(
            "Ignoring unusable school timezone %r (%s) — assuming the daemon's "
            "local clock for naive due dates",
            value,
            exc,
        )
        return None


class DDLScheduler:
    """Evaluates task deadlines against countdown reminder thresholds."""

    def __init__(
        self,
        state_manager: DaemonStateManager,
        reminders: list[ReminderThreshold] | None = None,
        submission_checker: Callable[[str, str], bool] | None = None,
        completion_checker: Callable[[str, str], bool] | None = None,
        school_timezone: tzinfo | str | None = None,
    ):
        self.state_manager = state_manager
        self.submission_checker = completion_checker or submission_checker
        self.completion_checker = self.submission_checker
        self.school_timezone = resolve_school_timezone(school_timezone)
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

            due_dt = parse_due_date(due_str, school_tz=self.school_timezone)
            if due_dt is None:
                continue

            # Ensure due_dt and task_now have matching tzinfo without mutating current_time
            task_now = current_time
            if due_dt.tzinfo is None:
                # Unreachable while parse_due_date keeps returning aware
                # datetimes, but kept so a future caller that hands back a naive
                # value still lands on the school clock rather than the host's.
                assumed = self.school_timezone or task_now.tzinfo
                if assumed is not None:
                    due_dt = due_dt.replace(tzinfo=assumed)
                    if task_now.tzinfo is None:
                        task_now = task_now.replace(tzinfo=assumed)
            elif task_now.tzinfo is None:
                task_now = task_now.replace(tzinfo=due_dt.tzinfo)

            minutes_left = (due_dt - task_now).total_seconds() / 60.0

            # If deadline has passed or task is already submitted or graded, skip reminders
            if minutes_left <= 0:
                continue

            if is_task_submitted_or_graded(task):
                continue

            status = str(task.get("status", "not-submitted")).lower()
            c_id = task.get("class_id")
            if not c_id:
                link = task.get("link") or task.get("url") or ""
                m_cls = re.search(r"/student/classes/(\d+)/", link)
                if m_cls:
                    c_id = m_cls.group(1)
                    task["class_id"] = c_id

            checked_live = False
            for reminder in self.reminders:
                if minutes_left <= reminder.threshold_minutes:
                    if not self.state_manager.is_reminder_dispatched(
                        task_id, reminder.name
                    ):
                        # Live-verify on ManageBac if student submitted or task was graded in the meantime
                        if not checked_live and self.submission_checker:
                            checked_live = True
                            if c_id:
                                try:
                                    if self.submission_checker(str(c_id), str(task_id)):
                                        updated_task = self.state_manager.get_task(task_id) or task
                                        if not is_task_submitted_or_graded(updated_task):
                                            task["status"] = "submitted"
                                            self.state_manager.update_task(task)
                                        else:
                                            task.update(updated_task)
                                        for th in self.reminders:
                                            self.state_manager.mark_reminder_dispatched(task_id, th.name)
                                        log.info("Task %s live-verified as submitted or graded — suppressing reminders", task_id)
                                        break
                                except Exception as exc:
                                    log.debug("Live submission check error for task %s: %s", task_id, exc)
                        if auto_mark:
                            self.state_manager.mark_reminder_dispatched(
                                task_id, reminder.name
                            )
                        t_id = int(task_id) if str(task_id).isdigit() else task_id
                        raw_c_id = c_id
                        num_c_id = int(raw_c_id) if raw_c_id is not None and str(raw_c_id).isdigit() else raw_c_id

                        task_url = task.get("url") or task.get("link")
                        if not task_url and num_c_id and t_id:
                            task_url = f"/student/classes/{num_c_id}/core_tasks/{t_id}"

                        cat = task.get("category")
                        if not cat:
                            labels = task.get("labels")
                            if isinstance(labels, list) and labels:
                                cat = labels[0]

                        event_data = standardize_task_payload(
                            {
                                "task_id": t_id,
                                "class_id": num_c_id,
                                "class_name": task.get("class_name"),
                                "title": task.get("title", ""),
                                "due_date": due_str,
                                "due_iso": due_dt.isoformat(),
                                "has_submit_button": task.get(
                                    "has_submit_button", False
                                ),
                                "category": cat,
                                "status": status,
                                "grade_letter": task.get("grade_letter"),
                                "grade_score": task.get("grade_score"),
                                "url": task_url,
                                "time_remaining_minutes": round(minutes_left, 1),
                                "reminder_threshold": reminder.name,
                            }
                        )

                        event = MBEvent(
                            event="deadline_approaching",
                            event_id=f"evt_{task_id}_reminder_{reminder.name}",
                            timestamp=task_now.isoformat(),
                            data=event_data,
                        )
                        events.append(event)
                        log.info(
                            "Dispatched %s deadline reminder for task %s (%s)",
                            reminder.name,
                            task_id,
                            task.get("title"),
                        )

        return events
