"""Unified task status, submission, grading, and presentation domain model."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any


class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    PENDING = "pending"
    NONE = "none"


class GradeStatus(str, Enum):
    GRADED = "graded"
    NOT_ASSESSED = "not_assessed"
    NOT_APPLICABLE = "not_applicable"


class LifecycleStatus(str, Enum):
    TODO = "todo"
    COMPLETED = "completed"


def is_submitted_badge(badge: str) -> bool:
    """Return True if badge indicates submission without negative qualifiers ('not', 'un')."""
    b = str(badge or "").strip().lower()
    return "submitted" in b and "not" not in b and "un" not in b


def get_submission_status(task: dict[str, Any]) -> SubmissionStatus:
    """Evaluate canonical submission status of a task."""
    status = str(task.get("status") or "").strip().lower()
    if status == "submitted":
        return SubmissionStatus.SUBMITTED

    labels = (task.get("labels") or [])
    detail = task.get("detail") or {}
    detail_labels = (detail.get("labels") or [])
    all_labels = labels + detail_labels
    if any(is_submitted_badge(l) for l in all_labels):
        return SubmissionStatus.SUBMITTED

    if str(detail.get("status") or "").strip().lower() == "submitted":
        return SubmissionStatus.SUBMITTED
    if detail.get("submission") or detail.get("submissions"):
        return SubmissionStatus.SUBMITTED

    # Check pending submission entrance
    has_submit_btn = bool(
        task.get("has_submit_button", False)
        or detail.get("has_submit_button", False)
    )
    if status == "not-submitted" or has_submit_btn:
        return SubmissionStatus.PENDING

    return SubmissionStatus.NONE


def is_task_submitted(task: dict[str, Any]) -> bool:
    """Return True if the task has been submitted."""
    return get_submission_status(task) == SubmissionStatus.SUBMITTED


def get_grade_status(task: dict[str, Any]) -> GradeStatus:
    """Evaluate canonical grading status of a task."""
    detail = task.get("detail") or {}
    letter = str(task.get("grade_letter") or detail.get("grade_letter") or "").strip()
    score = str(task.get("grade_score") or detail.get("grade_score") or "").strip()

    if letter == "-":
        letter = ""
    if score == "-":
        score = ""

    labels = (task.get("labels") or []) + (detail.get("labels") or [])
    labels_lower = [str(l).lower() for l in labels]

    # Explicit N/A or Exempt
    if letter.lower() in ("n/a", "not applicable", "exempt", "excused") or any(
        l in ("exempt", "excused") for l in labels_lower
    ):
        return GradeStatus.NOT_APPLICABLE

    # Explicit Not Assessed Yet
    if "not assessed yet" in letter.lower() or "not assessed yet" in labels_lower:
        if not (score and not re.match(r"^\s*0\s*/", score)):
            return GradeStatus.NOT_ASSESSED

    # 0 score placeholder
    if score and re.match(r"^\s*0\s*/", score) and not letter:
        return GradeStatus.NOT_ASSESSED

    # Valid evaluated grade or score
    has_score = bool(score)
    has_letter = bool(letter and letter.lower() not in ("not assessed", "not assessed yet"))
    if has_score or has_letter:
        return GradeStatus.GRADED

    return GradeStatus.NOT_ASSESSED


def is_task_todo(task: dict[str, Any]) -> bool:
    """Return True if task is uncompleted (unsubmitted and ungraded, with pending submission)."""
    if is_task_submitted(task):
        return False
    grade_status = get_grade_status(task)
    if grade_status == GradeStatus.GRADED or grade_status == GradeStatus.NOT_APPLICABLE:
        return False

    sub_status = get_submission_status(task)
    return sub_status == SubmissionStatus.PENDING


def is_task_completed(task: dict[str, Any]) -> bool:
    """Return True if a task is completed (either submitted or graded)."""
    return not is_task_todo(task)


def classify_task_view(task: dict[str, Any], now_ref: datetime | None = None) -> str:
    """Classify a task into 'upcoming', 'overdue', or 'past' based on due date and todo status."""
    from .client import parse_due_date

    now = now_ref or datetime.now()
    due_date = task.get("due_date")
    if isinstance(due_date, datetime):
        due_dt = due_date
    elif due_date:
        due_dt = parse_due_date(due_date, now_ref=now)
    else:
        due_dt = None

    if due_dt:
        if due_dt.tzinfo is not None and now.tzinfo is None:
            due_dt = due_dt.astimezone().replace(tzinfo=None)
        elif due_dt.tzinfo is None and now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)

        if due_dt > now:
            return "upcoming"

    if is_task_todo(task):
        return "overdue"
    return "past"


def get_task_display_grade(
    task: dict[str, Any],
    now_ref: datetime | None = None,
    standalone: bool = False,
) -> str:
    """Format single unified grade display string.

    If standalone is True (e.g. mb view), returns only the grade value
    ('A', 'A (100 / 100 pts)', 'N/A', or 'None').
    If standalone is False (mb list), returns the combined list column display
    ('A ( 100 / 100 pts )', 'Ungraded', 'Unsubmitted', '⚠ Unsubmitted', 'N/A').
    """
    from .client import parse_due_date

    detail = task.get("detail") or {}
    letter = str(task.get("grade_letter") or detail.get("grade_letter") or "").strip()
    score = str(task.get("grade_score") or detail.get("grade_score") or "").strip()
    if letter == "-":
        letter = ""
    if score == "-":
        score = ""

    grade_status = get_grade_status(task)
    if grade_status == GradeStatus.GRADED:
        if score and letter and letter.lower() not in ("not assessed yet", "not assessed", "n/a"):
            return f"{letter} ({score})" if standalone else f"{letter} ( {score} )"
        if score:
            return score
        if letter:
            return letter

    if grade_status == GradeStatus.NOT_APPLICABLE:
        return "N/A"

    if standalone:
        return "None"

    # Not graded yet
    sub_status = get_submission_status(task)
    if sub_status != SubmissionStatus.PENDING:
        return "Ungraded"

    # Unsubmitted / pending
    now = now_ref or datetime.now()
    due_date = task.get("due_date")
    due_dt = parse_due_date(due_date, now_ref=now) if due_date else None
    if due_dt:
        if due_dt.tzinfo is not None and now.tzinfo is None:
            due_dt = due_dt.astimezone().replace(tzinfo=None)
        elif due_dt.tzinfo is None and now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)

        if due_dt < now:
            return "⚠ Unsubmitted"
    return "Unsubmitted"


def get_task_display_status(task: dict[str, Any]) -> str:
    """Format single unified status string (matching mb view status)."""
    if is_task_todo(task):
        return "Incomplete (Todo)"
    if is_task_submitted(task):
        return "Complete (Submitted)"
    if get_grade_status(task) == GradeStatus.GRADED:
        return "Complete (Graded)"
    return "Complete"
