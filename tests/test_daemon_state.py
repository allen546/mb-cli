"""Tests for daemon state manager."""

from pathlib import Path
from mb_cli.daemon.state import DaemonStateManager


def test_state_manager_lifecycle(tmp_path: Path):
    state_file = tmp_path / "daemon_state.json"
    mgr = DaemonStateManager(state_file)

    assert not mgr.is_notification_processed(1001)
    mgr.mark_notification_processed(1001)
    assert mgr.is_notification_processed(1001)

    assert not mgr.is_reminder_dispatched(2002, "1h")
    mgr.mark_reminder_dispatched(2002, "1h")
    assert mgr.is_reminder_dispatched(2002, "1h")

    mgr.update_task({"id": "3003", "title": "Math HW", "status": "not-submitted"})
    assert mgr.get_task(3003)["title"] == "Math HW"

    mgr.save()
    assert state_file.exists()

    mgr2 = DaemonStateManager(state_file)
    assert mgr2.is_notification_processed(1001)
    assert mgr2.is_reminder_dispatched(2002, "1h")
    assert mgr2.get_task(3003)["title"] == "Math HW"
