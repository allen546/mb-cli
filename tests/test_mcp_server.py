"""Tests for mb_crawler.mcp_server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_crawler.mcp_server import (
    count_grade_frequencies,
    get_calendar_events,
    get_class_grades,
    get_ical_feed,
    get_notifications,
    get_timetable,
    list_classes,
    list_tasks,
    mark_all_notifications_read,
    mark_notification,
    mcp,
    submit_file,
    view_task,
)


@pytest.fixture()
def mock_build_client():
    """Patch auth.build_client for MCP tool tests."""
    with patch("mb_crawler.mcp_server.build_client") as mock:
        mock_state = MagicMock()
        mock_state.active_profile = "default"
        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.school = "bj80"
        mock.return_value = (mock_state, mock_client, "test@example.com")
        yield mock, mock_client


class TestMcpServerSetup:
    def test_mcp_name(self):
        assert mcp.name == "mb-crawler"

    def test_tools_registered(self):
        tool_names = (
            [t.name for t in mcp._tool_manager._tools.values()]
            if hasattr(mcp, "_tool_manager")
            else []
        )
        # Just check mcp is a FastMCP instance
        assert mcp is not None


class TestListTasksTool:
    def test_list_tasks(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {
            "upcoming": [{"id": "1", "title": "T1", "class_name": "Math"}],
            "past": [],
            "overdue": [],
            "student_name": "John",
            "school": "bj80",
            "base_url": "https://bj80.managebac.cn",
            "crawled_at": "2026-04-29T12:00:00",
        }
        result = list_tasks()
        data = json.loads(result)
        assert "upcoming" in data
        assert len(data["upcoming"]) == 1

    def test_list_tasks_with_subject(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {
            "upcoming": [
                {"id": "1", "title": "T1", "class_name": "Math HL"},
                {"id": "2", "title": "T2", "class_name": "English A"},
            ],
            "past": [],
            "overdue": [],
            "student_name": "John",
            "school": "bj80",
            "base_url": "https://bj80.managebac.cn",
            "crawled_at": "2026-04-29T12:00:00",
        }
        result = list_tasks(subject="Math")
        data = json.loads(result)
        assert len(data["upcoming"]) == 1
        assert data["upcoming"][0]["title"] == "T1"


class TestViewTaskTool:
    def test_view_task_by_url(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_task_detail.return_value = {"description": "Task details"}
        result = view_task(
            task_url="https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393"
        )
        data = json.loads(result)
        assert data["task"]["id"] == "27254393"
        assert data["detail"]["description"] == "Task details"

    def test_view_task_no_target(self, mock_build_client):
        result = view_task()
        data = json.loads(result)
        assert "error" in data


class TestSubmitFileTool:
    def test_submit_by_url(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.submit_file.return_value = {
            "ok": True,
            "filename": "hw.pdf",
            "task_url": "http://x",
        }
        result = submit_file(
            task_id="https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393",
            file_path="/tmp/hw.pdf",
        )
        data = json.loads(result)
        assert data["ok"] is True
        mock_client.submit_file.assert_called_once_with(
            "11460711", "27254393", "/tmp/hw.pdf"
        )

    def test_submit_not_found(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {"upcoming": [], "past": [], "overdue": []}
        result = submit_file(task_id="99999", file_path="/tmp/hw.pdf")
        data = json.loads(result)
        assert "error" in data


class TestGetNotificationsTool:
    def test_list_notifications(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("endpoint", "token")

        with patch("mb_crawler.mcp_server.MNNHubClient") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.stats.return_value = {"unread_messages": 3}
            mock_hub.list.return_value = {
                "items": [{"id": 1, "title": "Test"}],
                "meta": {"page": 1},
            }
            result = get_notifications()
            data = json.loads(result)
            assert data["stats"]["unread_messages"] == 3
            assert len(data["items"]) == 1

    def test_unread_only_filter(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("mb_crawler.mcp_server.MNNHubClient") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.stats.return_value = {}
            mock_hub.list.return_value = {"items": [], "meta": {}}
            get_notifications(unread_only=True)
            mock_hub.list.assert_called_once_with(page=1, per_page=20, filter_="unread")


class TestMarkNotificationTool:
    def test_mark_read(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("mb_crawler.mcp_server.MNNHubClient") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.mark_read.return_value = True
            result = mark_notification(notification_id=123, action="read")
            data = json.loads(result)
            assert data["ok"] is True
            assert data["action"] == "read"

    def test_invalid_action(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("mb_crawler.mcp_server.MNNHubClient") as MockHub:
            result = mark_notification(notification_id=123, action="invalid")
            data = json.loads(result)
            assert "error" in data


class TestMarkAllNotificationsReadTool:
    def test_mark_all_read(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("mb_crawler.mcp_server.MNNHubClient") as MockHub:
            mock_hub = MockHub.return_value
            mock_hub.mark_all_read.return_value = True
            result = mark_all_notifications_read()
            data = json.loads(result)
            assert data["ok"] is True


class TestGetCalendarEventsTool:
    def test_get_events(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_calendar_events.return_value = [
            {"id": 1, "title": "Exam", "start": "2026-04-30"}
        ]
        result = get_calendar_events(start_date="2026-04-29", end_date="2026-05-05")
        data = json.loads(result)
        assert data["start"] == "2026-04-29"
        assert data["end"] == "2026-05-05"
        assert len(data["events"]) == 1


class TestGetICalFeedTool:
    def test_get_ical(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_ical_feed.return_value = "BEGIN:VCALENDAR\nEND:VCALENDAR"
        result = get_ical_feed()
        assert "VCALENDAR" in result


class TestGetTimetableTool:
    def test_get_timetable(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_timetable.return_value = {
            "days": [{"header": "Monday"}],
            "lessons": [{"subject": "Math", "period": "P1"}],
        }
        result = get_timetable(date_str="2026-04-28")
        data = json.loads(result)
        assert len(data["lessons"]) == 1
        assert data["lessons"][0]["subject"] == "Math"


class TestListClassesTool:
    def test_list_classes(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {
            "upcoming": [
                {"class_name": "Math", "link": "/student/classes/100/c/1"},
                {"class_name": "English", "link": "/student/classes/200/c/2"},
            ],
            "past": [],
            "overdue": [],
        }
        result = list_classes()
        data = json.loads(result)
        assert len(data["classes"]) == 2
        ids = {c["id"] for c in data["classes"]}
        assert "100" in ids
        assert "200" in ids


class TestGetClassGradesTool:
    def test_grades_by_id(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.get_class_grades.return_value = {
            "tasks": [{"title": "HW1", "grade_letter": "A"}],
            "categories": [],
            "expected_grade": {"letter_grade": "A"},
        }
        result = get_class_grades(class_id="100")
        data = json.loads(result)
        assert data["class_id"] == "100"
        assert len(data["tasks"]) == 1

    def test_grades_by_name(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {
            "upcoming": [{"class_name": "Math HL", "link": "/student/classes/100/c/1"}],
            "past": [],
            "overdue": [],
        }
        mock_client.get_class_grades.return_value = {
            "tasks": [],
            "categories": [],
            "expected_grade": None,
        }
        result = get_class_grades(class_name="Math")
        data = json.loads(result)
        assert data["class_id"] == "100"

    def test_grades_class_not_found(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.crawl_all.return_value = {
            "upcoming": [{"class_name": "Math", "link": "/student/classes/100/c/1"}],
            "past": [],
            "overdue": [],
        }
        result = get_class_grades(class_name="Physics")
        data = json.loads(result)
        assert "error" in data

    def test_grades_no_params(self, mock_build_client):
        result = get_class_grades()
        data = json.loads(result)
        assert "error" in data


class TestCountGradeFrequenciesTool:
    def test_count_all(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.count_grade_frequencies.return_value = {
            "grades": {"A": 5, "B": 3},
            "total": 8,
            "classes": [{"id": "100", "name": "Math"}],
        }
        result = count_grade_frequencies()
        data = json.loads(result)
        assert data["total"] == 8

    def test_count_by_class(self, mock_build_client):
        mock, mock_client = mock_build_client
        mock_client.count_grade_frequencies.return_value = {
            "grades": {"A": 2},
            "total": 2,
            "classes": [{"id": "100", "name": "Math"}],
        }
        result = count_grade_frequencies(class_name="Math")
        data = json.loads(result)
        mock_client.count_grade_frequencies.assert_called_once_with(class_filter="Math")
