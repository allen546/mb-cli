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
        "body": '<p>Updated <a href="https://school.managebac.cn/student/classes/1000001/core_tasks/1000015">Task</a></p><p>When: September 10, 2026 at 9:10 AM</p>',
        "body_preview": "Updated Task NAME LIST",
        "sender": {"name": "Teacher Name"},
        "origin": {"name": "Physics Class"},
    }

    event = provider.normalize_notification(raw_item)
    assert event.event == "task_updated"
    assert event.event_id == "notif_244677168"
    assert event.data["task_id"] == 1000015
    assert event.data["class_id"] == 1000001
    assert event.data["due_date"] == "September 10, 2026 at 9:10 AM"
    assert event.data["url"] == "https://school.managebac.cn/student/classes/1000001/core_tasks/1000015"


def test_mnnhub_provider_extracts_task_title_from_updated_task_body():
    mock_client = MagicMock()
    provider = MNNHubProvider(mock_client)

    raw_item = {
        "id": 246223933,
        "title": "Updated Task",
        "event_name": "task_updated",
        "created_at": "2026-09-11T02:00:00.000Z",
        "body": '<p style="margin:0 0 10px"><strong style="font-weight:600">A. Teacher</strong> has just updated the Task <strong style="font-weight:600">Materials Check</strong> in <a href="https://demo-school.managebac.cn/student/classes/1000010/calendar">AP English Language Arts I (Hons) - Group 2 (Grade 10)</a>.</p> <p style="margin:0 0 10px"> <strong style="font-weight:600">When:</strong> September 11, 2026 at 12:10 PM </p> <p style="margin:0 0 10px"><a href="https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000017">View full details</a></p>',
        "body_preview": "A. Teacher has just updated the Task Materials Check in AP English Language Arts I (Hons) - Group 2 (Grade 10). When: September 11, 2026 at 12:10 PM View full details",
        "sender": {"name": "A. Teacher"},
        "origin": {"name": "AP English Language Arts I (Hons) - Group 2 (Grade 10)"},
    }

    event = provider.normalize_notification(raw_item)
    assert event.event == "task_updated"
    assert event.data["task_title"] == "Materials Check"


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


def test_mnnhub_provider_session_relogin():
    mock_client = MagicMock()
    # First call to get_notification_token raises session expired RuntimeError, second succeeds
    mock_client.get_notification_token.side_effect = [
        RuntimeError("Session expired or invalid — redirected to login"),
        ("https://mnn-hub.prod.faria.cn", "new_jwt_token"),
    ]
    relogin_mock = MagicMock(return_value=True)

    provider = MNNHubProvider(mock_client, auth_refresh_fn=relogin_mock)
    provider.start()

    assert relogin_mock.called
    assert provider.token == "new_jwt_token"
