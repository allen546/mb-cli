"""Tests for deadline countdown scheduler."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from mb_cli.daemon.events import ReminderThreshold
from mb_cli.daemon.scheduler import DDLScheduler
from mb_cli.daemon.state import DaemonStateManager


def test_scheduler_triggers_approaching_ddl(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    # Task due in 45 minutes
    due_dt = now + timedelta(minutes=45)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    mgr.update_task(
        {
            "id": "100",
            "title": "History Essay",
            "due_date": due_str,
            "status": "not-submitted",
        }
    )

    reminders = [
        ReminderThreshold(threshold_minutes=1440, name="24h"),
        ReminderThreshold(threshold_minutes=60, name="1h"),
        ReminderThreshold(threshold_minutes=15, name="15m"),
    ]
    scheduler = DDLScheduler(mgr, reminders)

    # 45 minutes remaining means 24h and 1h thresholds are crossed
    events = scheduler.evaluate_deadlines(now=now)
    assert len(events) == 2
    event_names = [e.data["reminder_threshold"] for e in events]
    assert "24h" in event_names
    assert "1h" in event_names

    # Evaluating again with same state shouldn't trigger duplicate events
    events_second = scheduler.evaluate_deadlines(now=now)
    assert len(events_second) == 0


def test_scheduler_silences_submitted_tasks(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=30)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    mgr.update_task(
        {
            "id": "200",
            "title": "Submitted Math HW",
            "due_date": due_str,
            "status": "submitted",
        }
    )

    scheduler = DDLScheduler(mgr)
    events = scheduler.evaluate_deadlines(now=now)
    assert len(events) == 0


def test_scheduler_silences_tasks_with_submitted_labels_or_grades(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=30)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Task has NO 'status' field, but has 'Submitted' in labels and grade_score
    mgr.update_task(
        {
            "id": "1000099",
            "title": "Homework of summer holiday",
            "due_date": due_str,
            "labels": ["Formative", "Homework", "Submitted"],
            "grade_score": "Submitted",
        }
    )

    scheduler = DDLScheduler(mgr)
    events = scheduler.evaluate_deadlines(now=now)
    assert len(events) == 0


def test_scheduler_extracts_class_id_from_link_for_live_check(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=30)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Task has NO class_id field, but has class_id in link
    mgr.update_task(
        {
            "id": "1000020",
            "title": "Poster",
            "link": "https://demo-school.managebac.cn/student/classes/1000012/core_tasks/1000020",
            "due_date": due_str,
            "labels": ["Formative", "Pending"],
        }
    )

    checked_calls = []

    def mock_checker(class_id: str, task_id: str) -> bool:
        checked_calls.append((class_id, task_id))
        return True  # student submitted live

    scheduler = DDLScheduler(mgr, submission_checker=mock_checker)
    events = scheduler.evaluate_deadlines(now=now)

    assert len(events) == 0  # silenced!
    assert checked_calls == [("1000012", "1000020")]
    assert mgr.get_task("1000020")["status"] == "submitted"


def test_scheduler_emits_standardized_deadline_approaching_payload(tmp_path: Path):
    from mb_cli.daemon.events import STANDARD_TASK_FIELDS

    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=45)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")

    mgr.update_task(
        {
            "id": "1000014",
            "task_id": "1000014",
            "class_id": "1000010",
            "class_name": "English Language Arts I (Hons)",
            "title": "Vocab Quiz 2",
            "due_date": due_str,
            "status": "not-submitted",
            "has_submit_button": False,
            "labels": ["Quiz"],
            "url": "https://example.managebac.cn/student/classes/1000010/core_tasks/1000014",
        }
    )

    reminders = [
        ReminderThreshold(threshold_minutes=60, name="1h"),
    ]
    scheduler = DDLScheduler(mgr, reminders)
    events = scheduler.evaluate_deadlines(now=now)

    assert len(events) == 1
    ev = events[0]
    assert ev.event == "deadline_approaching"
    assert ev.validate() is True

    for f in STANDARD_TASK_FIELDS:
        assert f in ev.data, f"Missing standard field: {f}"

    assert ev.data["task_id"] == 1000014
    assert ev.data["class_id"] == 1000010
    assert ev.data["class_name"] == "English Language Arts I (Hons)"
    assert ev.data["title"] == "Vocab Quiz 2"
    assert ev.data["due_date"] == due_str
    assert ev.data["due_iso"] is not None
    assert ev.data["has_submit_button"] is False
    assert ev.data["category"] == "Quiz"
    assert ev.data["status"] == "not-submitted"
    assert ev.data["grade_letter"] is None
    assert ev.data["grade_score"] is None
    assert ev.data["url"] == "https://example.managebac.cn/student/classes/1000010/core_tasks/1000014"
    assert ev.data["time_remaining_minutes"] == 45.0
    assert ev.data["reminder_threshold"] == "1h"


