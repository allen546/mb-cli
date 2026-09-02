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
