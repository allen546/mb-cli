"""Tests for suppressing notifications on submitted or graded tasks."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mb_cli.daemon.events import DaemonConfig, MBEvent, ReminderThreshold, WebhookConfig
from mb_cli.daemon.provider import AbstractNotificationProvider
from mb_cli.daemon.scheduler import DDLScheduler
from mb_cli.daemon.service import DaemonService
from mb_cli.daemon.state import DaemonStateManager
from mb_cli.task_status import (
    GradeStatus,
    get_grade_status,
    is_task_graded,
    is_task_submitted,
    is_task_submitted_or_graded,
)


class MockProvider(AbstractNotificationProvider):
    def __init__(self, events: list[MBEvent] | None = None):
        self.events = events or []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def poll_events(self) -> list[MBEvent]:
        ev = list(self.events)
        self.events.clear()
        return ev

    def refresh_auth(self) -> bool:
        return True


def test_task_status_is_task_graded_and_submitted_or_graded():
    # 1. Graded with score, not submitted
    task1 = {"grade_score": "7/7", "status": "not-submitted"}
    assert is_task_graded(task1) is True
    assert is_task_submitted(task1) is False
    assert is_task_submitted_or_graded(task1) is True

    # 2. Graded with letter, not submitted
    task2 = {"grade_letter": "A", "status": "not-submitted"}
    assert is_task_graded(task2) is True
    assert is_task_submitted_or_graded(task2) is True

    # 3. Explicit N/A or Exempt
    task3 = {"grade_letter": "N/A", "status": "not-submitted"}
    assert is_task_graded(task3) is True
    assert is_task_submitted_or_graded(task3) is True

    # 4. Ungraded / Pending
    task4 = {"status": "not-submitted", "grade_score": "-", "grade_letter": "-"}
    assert is_task_graded(task4) is False
    assert is_task_submitted(task4) is False
    assert is_task_submitted_or_graded(task4) is False

    # 5. Submitted, not yet graded
    task5 = {"status": "submitted", "grade_score": "", "grade_letter": ""}
    assert is_task_graded(task5) is False
    assert is_task_submitted(task5) is True
    assert is_task_submitted_or_graded(task5) is True


def test_scheduler_skips_graded_task_in_cache(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=30)
    # isoformat() keeps the offset: parse_due_date returns aware datetimes,
    # and a strftime'd string carrying no offset would be read as the host's
    # local time rather than this test's UTC frame.
    due_str = due_dt.isoformat()

    # Task is graded (7/7) but NOT submitted (e.g. offline assignment or graded early)
    mgr.update_task(
        {
            "id": "300",
            "title": "Oral Presentation",
            "due_date": due_str,
            "status": "not-submitted",
            "grade_score": "7/7",
            "grade_letter": "7",
        }
    )

    scheduler = DDLScheduler(mgr)
    events = scheduler.evaluate_deadlines(now=now)
    assert len(events) == 0, "Scheduler should not dispatch reminders for a graded task"


def test_scheduler_live_check_detects_graded_task(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mgr = DaemonStateManager(state_file)

    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    due_dt = now + timedelta(minutes=30)
    due_str = due_dt.isoformat()

    # In cache, task is not submitted and not graded yet
    mgr.update_task(
        {
            "id": "301",
            "title": "Lab Practical",
            "class_id": "1000014",
            "due_date": due_str,
            "status": "not-submitted",
        }
    )

    checked_calls = []

    def mock_checker(class_id: str, task_id: str) -> bool:
        checked_calls.append((class_id, task_id))
        # Live check discovers the task was already graded
        task = mgr.get_task(task_id)
        task["grade_score"] = "100/100"
        task["grade_letter"] = "A"
        mgr.update_task(task)
        return True

    scheduler = DDLScheduler(mgr, submission_checker=mock_checker)
    events = scheduler.evaluate_deadlines(now=now)

    assert len(events) == 0, "Reminder should be suppressed when live checker discovers grade"
    assert checked_calls == [("1000014", "301")]
    # Task should not be blindly marked as submitted if it was graded
    assert mgr.get_task("301")["status"] != "submitted"
    assert mgr.get_task("301")["grade_score"] == "100/100"


def test_service_pre_alarm_check_with_graded_stealth_details(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    mock_client.get_submissions.return_value = []  # No dropbox submission
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    now = datetime.now().astimezone()
    due_dt = now + timedelta(minutes=45)
    task = {
        "id": "888",
        "task_id": "888",
        "class_id": "1000014",
        "title": "In-Class Essay",
        "due_date": due_dt.isoformat(),
        "status": "not-submitted",
        "has_submit_button": False,
    }
    state_mgr.update_task(task)

    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=MockProvider([]),
    )

    # Mock stealth crawler returning live details that are GRADED (not submitted)
    service.stealth_crawler.fetch_task_details = MagicMock(
        return_value={
            "id": "888",
            "task_id": "888",
            "class_id": "1000014",
            "title": "In-Class Essay",
            "due_date": due_dt.isoformat(),
            "status": "not-submitted",
            "has_submit_button": False,
            "grade_letter": "A",
            "grade_score": "95 / 100",
        }
    )

    res = service.run_check_cycle()
    assert res["reminders_dispatched"] == 0, "Reminder must be suppressed because live task is graded"
    cached = state_mgr.get_task("888")
    assert cached.get("grade_score") == "95 / 100"
    assert cached.get("grade_letter") == "A"


def test_service_suppresses_task_updated_when_already_submitted_or_graded(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    # Pre-populate state: task 901 is already graded
    state_mgr.update_task(
        {
            "id": "901",
            "class_id": "100",
            "title": "Chemistry Quiz",
            "status": "not-submitted",
            "grade_score": "10/10",
            "grade_letter": "10",
        }
    )

    # Notification arrives: "task_updated" for task 901
    mock_event = MBEvent(
        event="task_updated",
        data={
            "notification_id": 12345,
            "title": "Updated Task: Chemistry Quiz",
            "task_id": 901,
            "class_id": 100,
        },
    )
    provider = MockProvider([mock_event])
    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=provider,
    )
    service.stealth_crawler.fetch_task_details = MagicMock(
        return_value={
            "id": "901",
            "class_id": "100",
            "title": "Chemistry Quiz",
            "status": "not-submitted",
            "grade_score": "10/10",
            "grade_letter": "10",
        }
    )
    service.dispatcher.dispatch = MagicMock(return_value=[{"success": True}])

    res = service.run_check_cycle()
    # Should NOT dispatch task_updated because task was already graded and grade didn't change
    assert res["new_notifications"] == 0
    service.dispatcher.dispatch.assert_not_called()
    assert state_mgr.is_notification_processed(12345)


def test_service_promotes_task_graded_on_first_grade(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    # Pre-populate state: task 902 was ungraded
    state_mgr.update_task(
        {
            "id": "902",
            "class_id": "100",
            "title": "English Essay",
            "status": "submitted",
        }
    )

    mock_event = MBEvent(
        event="task_updated",
        data={
            "notification_id": 12346,
            "title": "Updated Task: English Essay",
            "task_id": 902,
            "class_id": 100,
        },
    )
    provider = MockProvider([mock_event])
    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=provider,
    )
    service.stealth_crawler.fetch_task_details = MagicMock(
        return_value={
            "id": "902",
            "class_id": "100",
            "title": "English Essay",
            "status": "submitted",
            "grade_score": "98/100",
            "grade_letter": "A",
        }
    )
    dispatched_events = []
    service.dispatcher.dispatch = MagicMock(side_effect=lambda ev: dispatched_events.append(ev) or [{"success": True}])

    res = service.run_check_cycle()
    # Should dispatch because it is promoted to task_graded!
    assert res["new_notifications"] == 1
    assert len(dispatched_events) == 1
    assert dispatched_events[0].event == "task_graded"
    assert dispatched_events[0].data["grade_score"] == "98/100"


def test_service_promotes_task_created_to_task_graded_when_score_released(tmp_path: Path):
    """When a task is newly created and a score is released on the same run, promote to task_graded."""
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    mock_event = MBEvent(
        event="task_created",
        data={
            "notification_id": 99901,
            "title": "New Task: 语文早读小测1",
            "task_id": 1000099,
            "class_id": 1000014,
        },
    )
    provider = MockProvider([mock_event])
    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=provider,
    )
    service.stealth_crawler.fetch_task_details = MagicMock(
        return_value={
            "id": "1000099",
            "class_id": "1000014",
            "title": "语文早读小测1",
            "class_name": "AP Chinese Language Arts I 高一语文1班 (Grade 10) E103",
            "due_date": "Sep 11, 10:10 AM",
            "grade_letter": "A",
            "grade_score": "90 / 100 pts",
            "status": None,
        }
    )
    dispatched_events = []
    service.dispatcher.dispatch = MagicMock(side_effect=lambda ev: dispatched_events.append(ev) or [{"success": True}])

    res = service.run_check_cycle()
    assert res["new_notifications"] == 1
    assert len(dispatched_events) == 1
    # Must be promoted to task_graded instead of remaining task_created!
    assert dispatched_events[0].event == "task_graded"
    assert dispatched_events[0].data["grade_letter"] == "A"
    assert dispatched_events[0].data["grade_score"] == "90 / 100 pts"


