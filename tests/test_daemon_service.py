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
        "class_id": "1000014",
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
    mock_client.get_submissions.assert_called_once_with("1000014", "777")
    # Verify no general crawling was performed
    mock_client.get_tasks_by_view.assert_not_called()


def test_daemon_service_on_start_default(tmp_path: Path):
    mock_client = MagicMock()
    # Pre-populate state manager with an existing task
    state_mgr = DaemonStateManager(tmp_path / "state.json")
    state_mgr.update_task({"id": "111", "title": "Old Task", "due_date": "2026-09-10 10:00:00"})

    # Setup upcoming task return and terminate loop
    def _upcoming(view, max_pages=3):
        service._running = False
        return [
            {"id": "111", "title": "Old Task Updated", "due_date": "2026-09-10 12:00:00"},
            {"id": "222", "title": "Brand New Task", "due_date": "2026-09-12 15:00:00"},
        ]

    mock_client.get_tasks_by_view.side_effect = _upcoming

    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=MockProvider([]),
    )

    # Start service; default on_start should invoke sync_upcoming_tasks even though cache is non-empty
    service.start()

    mock_client.get_tasks_by_view.assert_called_once_with("upcoming", max_pages=3)
    # Verify existing task updated
    assert state_mgr.get_task("111")["title"] == "Old Task Updated"
    # Verify newly discovered task added to cache
    assert state_mgr.get_task("222") is not None
    assert state_mgr.get_task("222")["title"] == "Brand New Task"


def test_daemon_service_on_start_custom_callback(tmp_path: Path):
    mock_client = MagicMock()
    state_mgr = DaemonStateManager(tmp_path / "state.json")
    custom_hook = MagicMock()

    def _custom_callback(svc: DaemonService):
        custom_hook(svc)
        svc._running = False

    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=MockProvider([]),
        on_start=_custom_callback,
    )

    service.start()

    custom_hook.assert_called_once_with(service)
    # Default sync_upcoming_tasks was replaced by custom callback, so client was not called
    mock_client.get_tasks_by_view.assert_not_called()


def test_daemon_service_on_start_error_resilience(tmp_path: Path):
    mock_client = MagicMock()
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    def _failing_callback(svc: DaemonService):
        svc._running = False
        raise RuntimeError("Simulated connection failure during startup refresh")

    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=MockProvider([]),
        on_start=_failing_callback,
    )

    # service.start() should not crash even if on_start raises
    service.start()
    assert service._running is False


def test_daemon_service_protects_task_title_against_class_name(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    mock_event = MBEvent(
        event="task_created",
        data={
            "notification_id": 8888,
            "title": "New Task: Real Task Title",
            "task_title": "Real Task Title",
            "class_name": "AP Calc BC",
            "class_id": 12345,
            "task_id": 67890,
        },
    )
    provider = MockProvider([mock_event])
    service = DaemonService(
        client=mock_client,
        state_manager=state_mgr,
        provider=provider,
    )
    # Simulate stealth crawler accidentally returning class_name as title
    service.stealth_crawler.fetch_task_details = MagicMock(
        return_value={
            "id": "67890",
            "task_id": "67890",
            "title": "AP Calc BC",  # buggy scraper returned class name
            "class_name": "AP Calc BC",
            "due_date": "2026-09-15 23:59:00",
            "status": "not-submitted",
        }
    )
    dispatched: list[MBEvent] = []
    service.dispatcher.dispatch = MagicMock(side_effect=lambda ev: dispatched.append(ev) or [{"success": True}])

    res = service.run_check_cycle()
    assert res["new_notifications"] == 1
    assert len(dispatched) == 1
    assert dispatched[0].data["task_title"] == "Real Task Title"
    # Verify cached task title was preserved, not overwritten with class name
    cached_task = state_mgr.get_task("67890")
    assert cached_task is not None
    assert cached_task["title"] == "Real Task Title"


def test_daemon_service_standardizes_emitted_task_events(tmp_path: Path):
    from mb_cli.daemon.events import STANDARD_TASK_FIELDS

    mock_client = MagicMock()
    mock_client.base = "https://school.managebac.cn"
    mock_client.get_tasks_by_view.return_value = []
    state_mgr = DaemonStateManager(tmp_path / "state.json")

    mock_event = MBEvent(
        event="new_task",
        data={
            "notification_id": 1001,
            "title": "New Task: Literature Essay",
            "task_id": "55555",
            "class_id": "77777",
            "class_name": "IB English A",
            "due_date": "2026-09-30 23:59:00",
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
            "id": "55555",
            "task_id": "55555",
            "title": "Literature Essay",
            "class_id": "77777",
            "class_name": "IB English A",
            "due_date": "2026-09-30 23:59:00",
            "has_submit_button": True,
            "status": "not-submitted",
            "category": "Summative",
            "grade_letter": None,
            "grade_score": None,
            "url": "https://school.managebac.cn/student/classes/77777/core_tasks/55555",
        }
    )
    dispatched: list[MBEvent] = []
    service.dispatcher.dispatch = MagicMock(side_effect=lambda ev: dispatched.append(ev) or [{"success": True}])

    res = service.run_check_cycle()
    assert res["new_notifications"] == 1
    assert len(dispatched) == 1

    ev = dispatched[0]
    assert ev.event == "task_created"
    assert ev.validate() is True
    for f in STANDARD_TASK_FIELDS:
        assert f in ev.data, f"Missing standard field: {f}"

    assert ev.data["task_id"] == 55555
    assert ev.data["class_id"] == 77777
    assert ev.data["class_name"] == "IB English A"
    assert ev.data["title"] == "Literature Essay"
    assert ev.data["due_date"] == "2026-09-30 23:59:00"
    assert ev.data["due_iso"] is not None
    assert "2026-09-30" in ev.data["due_iso"]
    assert ev.data["has_submit_button"] is True
    assert ev.data["category"] == "Summative"
    assert ev.data["status"] == "not-submitted"
    assert ev.data["grade_letter"] is None
    assert ev.data["grade_score"] is None
    assert ev.data["url"] == "https://school.managebac.cn/student/classes/77777/core_tasks/55555"


# ── Active-hours gating ──────────────────────────────────────────────────

from datetime import datetime
from unittest.mock import patch


def _service(tmp_path: Path, **config_kwargs) -> DaemonService:
    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    return DaemonService(
        client=mock_client,
        config=DaemonConfig(**config_kwargs),
        state_manager=DaemonStateManager(tmp_path / "state.json"),
        provider=MockProvider([]),
    )


def test_no_windows_means_always_active(tmp_path: Path):
    """The default must stay poll-every-interval: gating is opt-in."""
    service = _service(tmp_path)
    assert service.config.active_windows == []
    assert service._in_active_window() is True


def test_window_excluding_now_blocks_polling(tmp_path: Path):
    # A window that already closed today (09:00-10:00 against a frozen 12:00).
    with patch("mb_cli.daemon._now_local", return_value=datetime(2026, 9, 18, 12, 0)):
        service = _service(tmp_path, active_windows=[["09:00", "10:00"]])
        assert service._in_active_window() is False


def test_window_containing_now_allows_polling(tmp_path: Path):
    with patch("mb_cli.daemon._now_local", return_value=datetime(2026, 9, 18, 12, 0)):
        service = _service(tmp_path, active_windows=[["09:00", "17:00"]])
        assert service._in_active_window() is True


def test_malformed_window_fails_open(tmp_path: Path):
    """A typo in daemon.json must not silently stop the notifier."""
    service = _service(tmp_path, active_windows=[["not-a-time", "23:00"]])
    assert service._in_active_window() is True


def test_start_does_not_poll_outside_active_window(tmp_path: Path):
    """`--active-hours-*` used to be accepted and then ignored by the loop."""
    polls: list[int] = []

    class _CountingProvider(MockProvider):
        def poll_events(self):
            polls.append(1)
            return []

    mock_client = MagicMock()
    mock_client.get_tasks_by_view.return_value = []
    service = DaemonService(
        client=mock_client,
        config=DaemonConfig(active_windows=[["09:00", "10:00"]]),
        state_manager=DaemonStateManager(tmp_path / "state.json"),
        provider=_CountingProvider([]),
    )

    def _sleep(seconds):
        # A real daemon would sleep for hours here; stop the test instead.
        service._running = False

    with (
        patch("mb_cli.daemon._now_local", return_value=datetime(2026, 9, 18, 12, 0)),
        patch("mb_cli.daemon.service.time.sleep", side_effect=_sleep),
    ):
        service.start()

    assert polls == []


def test_active_windows_round_trip_through_the_config():
    from mb_cli.daemon.events import DaemonConfig

    config = DaemonConfig.from_dict({"active_windows": [["09:00", "17:00"]]})
    assert config.active_windows == [["09:00", "17:00"]]
    assert config.to_dict()["active_windows"] == [["09:00", "17:00"]]
    # A missing or junk value means "no gating", not a crash.
    assert DaemonConfig.from_dict({}).active_windows == []
    assert DaemonConfig.from_dict({"active_windows": None}).active_windows == []
    assert DaemonConfig.from_dict({"active_windows": ["nonsense"]}).active_windows == []
