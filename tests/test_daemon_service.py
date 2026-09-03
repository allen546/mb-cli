"""Tests for main daemon service orchestration."""

from pathlib import Path
from unittest.mock import MagicMock
from mb_cli.daemon.events import DaemonConfig, MBEvent, WebhookConfig
from mb_cli.daemon.provider import AbstractNotificationProvider
from mb_cli.daemon.service import DaemonService
from mb_cli.daemon.state import DaemonStateManager


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


def test_daemon_service_check_cycle(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    mock_event = MBEvent(
        event="task_created",
        data={"notification_id": 9999, "title": "New Assignment"},
    )
    provider = MockProvider([mock_event])
    config = DaemonConfig(
        webhooks=[WebhookConfig(url="http://localhost:9999/wh")]
    )

    service = DaemonService(
        client=mock_client,
        config=config,
        state_manager=state_mgr,
        provider=provider,
    )
    service.dispatcher.dispatch = MagicMock(return_value=[{"success": True, "url": "http://localhost:9999/wh"}])

    res = service.run_check_cycle()
    assert res["new_notifications"] == 1
    assert state_mgr.is_notification_processed(9999)

    # Next check should not process already-processed notification
    res2 = service.run_check_cycle()
    assert res2["new_notifications"] == 0


def test_daemon_service_live_submission_check(tmp_path: Path):
    from datetime import datetime, timedelta

    mock_client = MagicMock()
    # Task dropbox has a submitted file
    mock_client.get_submissions.return_value = [{"name": "solution.pdf", "url": "/att/1"}]
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    now = datetime.now().astimezone()
    due_dt = now + timedelta(minutes=45)
    task = {
        "id": "777",
        "task_id": "777",
        "class_id": "11516148",
        "title": "Calculus Worksheet",
        "due_date": due_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "not-submitted",
        "has_submit_button": True,
    }
    state_mgr.update_task(task)

    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=MockProvider([]),
    )

    res = service.run_check_cycle()
    # Reminder should be suppressed because live check discovered submission!
    assert res["reminders_dispatched"] == 0
    # Cached task status should now be updated to submitted
    assert state_mgr.get_task("777")["status"] == "submitted"
    # Verify get_submissions was called ONLY for that specific task
    mock_client.get_submissions.assert_called_once_with("11516148", "777")
    # Verify no general crawling was performed
    mock_client.get_tasks_by_view.assert_not_called()
