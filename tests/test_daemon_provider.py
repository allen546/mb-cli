"""Tests for notification providers."""

from unittest.mock import MagicMock
import pytest
from mb_cli.daemon.provider import MNNHubProvider, MobilePushProvider
from mb_cli.daemon.events import MBEvent


def test_mnnhub_provider_normalization():
    mock_client = MagicMock()
    provider = MNNHubProvider(mock_client)

    raw_item = {
        "id": 244677168,
        "title": "Updated Task",
        "event_name": "task_updated",
        "created_at": "2026-09-02T07:44:54.316Z",
        "body": '<p>Updated <a href="https://school.managebac.cn/student/classes/1000012/core_tasks/1000099">Task</a></p><p>When: September 10, 2026 at 9:10 AM</p>',
        "body_preview": "Updated Task NAME LIST",
        "sender": {"name": "Teacher Name"},
        "origin": {"name": "Physics Class"},
    }

    event = provider.normalize_notification(raw_item)
    assert event.event == "task_updated"
    assert event.event_id == "notif_244677168"
    assert event.data["task_id"] == 1000099
    assert event.data["class_id"] == 1000012
    assert event.data["due_date"] == "September 10, 2026 at 9:10 AM"
    assert event.data["url"] == "https://school.managebac.cn/student/classes/1000012/core_tasks/1000099"


def test_mobile_push_provider():
    provider = MobilePushProvider()
    provider.start()
    assert provider.poll_events() == []

    evt = MBEvent(event="task_created", data={"title": "Mobile Task"})
    provider.push_event(evt)

    polled = provider.poll_events()
    assert len(polled) == 1
    assert polled[0].event == "task_created"
    assert provider.poll_events() == []
    provider.stop()
