"""Tests for mb_cli.formatters."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from mb_cli.formatters import (
    error,
    ok,
    print_payload,
    render_pretty,
    resolve_format,
)


class TestResolveFormat:
    def test_explicit_json(self):
        assert resolve_format("json") == "json"

    def test_explicit_pretty(self):
        assert resolve_format("pretty") == "pretty"

    def test_none_defaults_to_json_when_not_tty(self):
        with patch("mb_cli.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            assert resolve_format(None) == "json"

    def test_none_defaults_to_pretty_when_tty(self):
        with patch("mb_cli.formatters.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            assert resolve_format(None) == "pretty"


class TestOk:
    def test_structure(self):
        result = ok("list", "default", {"key": "value"})
        assert result["ok"] is True
        assert result["command"] == "list"
        assert result["profile"] == "default"
        assert result["data"]["key"] == "value"


class TestError:
    def test_structure(self):
        result = error("view", "not_found", "Task not found")
        assert result["ok"] is False
        assert result["command"] == "view"
        assert result["error"]["code"] == "not_found"
        assert result["error"]["message"] == "Task not found"


class TestRenderPretty:
    def test_error_payload(self):
        payload = error("test", "err_code", "Something broke")
        output = render_pretty(payload)
        assert "ERROR [err_code]" in output
        assert "Something broke" in output

    def test_login_success(self):
        payload = ok(
            "login",
            "default",
            {
                "school": "bj80",
                "domain": "managebac.cn",
                "email": "a@b.com",
                "base_url": "https://bj80.managebac.cn",
                "auth_method": "cookie",
            },
        )
        output = render_pretty(payload)
        assert "Login successful" in output
        assert "bj80" in output

    def test_logout_success(self):
        payload = ok("logout", "default", {"logged_out": True, "all_profiles": False})
        output = render_pretty(payload)
        assert "Logout complete" in output

    def test_list_with_tasks(self):
        payload = ok(
            "list",
            "default",
            {
                "meta": {
                    "student_name": "John",
                    "school": "bj80",
                    "view": "all",
                    "subject_filter": None,
                    "details": False,
                },
                "summary": {
                    "upcoming_count": 1,
                    "past_count": 0,
                    "overdue_count": 0,
                    "total_count": 1,
                },
                "tasks": {
                    "upcoming": [
                        {
                            "id": "1",
                            "title": "HW1",
                            "class_name": "Math",
                            "due_date": "Apr 1",
                            "grade_score": "A",
                        }
                    ],
                    "past": [],
                    "overdue": [],
                },
            },
        )
        output = render_pretty(payload)
        assert "Task list" in output
        assert "HW1" in output
        assert "[upcoming]" in output

    def test_list_empty(self):
        payload = ok(
            "list",
            "default",
            {
                "meta": {
                    "student_name": "John",
                    "school": "s",
                    "view": "all",
                    "subject_filter": None,
                    "details": False,
                },
                "summary": {
                    "upcoming_count": 0,
                    "past_count": 0,
                    "overdue_count": 0,
                    "total_count": 0,
                },
                "tasks": {"upcoming": [], "past": [], "overdue": []},
            },
        )
        output = render_pretty(payload)
        assert "Task list" in output
        assert "total: 0" in output

    def test_view_task(self):
        payload = ok(
            "view",
            "default",
            {
                "task": {
                    "id": "123",
                    "title": "Test Task",
                    "class_name": "Physics",
                    "due_date": "May 1",
                    "grade_score": "B",
                    "link": "http://x",
                },
                "detail": {
                    "description": "Do this",
                    "comments": ["Nice work"],
                    "attachments": [
                        {
                            "name": "f.pdf",
                            "url": "http://x/f.pdf",
                            "source": "description",
                        }
                    ],
                },
            },
        )
        output = render_pretty(payload)
        assert "Task detail" in output
        assert "Do this" in output
        assert "Nice work" in output
        assert "f.pdf" in output

    def test_submit(self):
        payload = ok(
            "submit", "default", {"filename": "hw.pdf", "task_url": "http://x"}
        )
        output = render_pretty(payload)
        assert "File submitted" in output
        assert "hw.pdf" in output

    def test_notifications(self):
        payload = ok(
            "notifications",
            "default",
            {
                "stats": {"unread_messages": 3},
                "items": [
                    {
                        "id": 1,
                        "title": "New grade",
                        "is_read": False,
                        "created_at": "2026-04-29T10:00:00",
                    }
                ],
                "meta": {"page": 1, "total_pages": 2, "total": 15},
            },
        )
        output = render_pretty(payload)
        assert "Notifications" in output
        assert "New grade" in output
        assert "*" in output

    def test_notifications_empty(self):
        payload = ok("notifications", "default", {"stats": {}, "items": [], "meta": {}})
        output = render_pretty(payload)
        assert "(none)" in output

    def test_notifications_mutate(self):
        payload = ok(
            "notifications.mutate",
            "default",
            {"action": "read", "notification_id": 123, "ok": True},
        )
        output = render_pretty(payload)
        assert "read" in output

    def test_calendar(self):
        payload = ok(
            "calendar",
            "default",
            {
                "start": "2026-04-29",
                "end": "2026-05-05",
                "events": [
                    {
                        "id": 1,
                        "title": "Exam",
                        "start": "2026-04-30T09:00:00",
                        "type": "exam",
                    }
                ],
            },
        )
        output = render_pretty(payload)
        assert "Calendar events" in output
        assert "Exam" in output

    def test_calendar_empty(self):
        payload = ok("calendar", "default", {"start": "x", "end": "y", "events": []})
        output = render_pretty(payload)
        assert "(no events)" in output

    def test_timetable(self):
        payload = ok(
            "timetable",
            "default",
            {
                "start_date": "2026-04-28",
                "days": [{"header": "Monday", "is_today": True}],
                "lessons": [
                    {
                        "period": "P1",
                        "day": "Monday",
                        "is_today": True,
                        "time": "08:00",
                        "subject": "Math",
                        "teacher": "Mr. S",
                        "room": "R1",
                        "year": "Y11",
                    }
                ],
            },
        )
        output = render_pretty(payload)
        assert "Timetable" in output
        assert "Math" in output

    def test_timetable_empty(self):
        payload = ok(
            "timetable", "default", {"start_date": "x", "days": [], "lessons": []}
        )
        output = render_pretty(payload)
        assert "(no lessons)" in output

    def test_grades(self):
        payload = ok(
            "grades",
            "default",
            {
                "class_id": "123",
                "tasks": [
                    {
                        "task_id": "1",
                        "title": "HW1",
                        "grade_letter": "A",
                        "points": "95/100",
                        "category": "HW",
                    }
                ],
                "categories": [{"name": "HW", "weight": 0.4}],
                "expected_grade": {
                    "letter_grade": "A",
                    "average_score": 4.5,
                    "num_graded": 2,
                },
            },
        )
        output = render_pretty(payload)
        assert "Class grades" in output
        assert "HW1" in output
        assert "40%" in output

    def test_grades_empty(self):
        payload = ok(
            "grades",
            "default",
            {"class_id": "1", "tasks": [], "categories": [], "expected_grade": None},
        )
        output = render_pretty(payload)
        assert "(no tasks)" in output

    def test_grades_list(self):
        payload = ok(
            "grades.list",
            "default",
            {
                "classes": [
                    {"id": "1", "name": "Math"},
                    {"id": "2", "name": "English"},
                ],
            },
        )
        output = render_pretty(payload)
        assert "Classes" in output
        assert "Math" in output
        assert "English" in output

    def test_unknown_command_falls_through_to_json(self):
        payload = {"ok": True, "command": "unknown_cmd", "data": {"x": 1}}
        output = render_pretty(payload)
        parsed = json.loads(output)
        assert parsed["data"]["x"] == 1


class TestPrintPayload:
    def test_json_format(self):
        payload = ok("login", "default", {"school": "bj80"})
        output = (
            print_payload.__wrapped__ if hasattr(print_payload, "__wrapped__") else None
        )
        # Test via StringIO capture
        captured = StringIO()
        with patch("builtins.print") as mock_print:
            print_payload(payload, None, "json")
            printed = mock_print.call_args[0][0]
            parsed = json.loads(printed)
            assert parsed["ok"] is True

    def test_pretty_format(self):
        payload = ok("login", "default", {"school": "bj80"})
        with patch("builtins.print") as mock_print:
            print_payload(payload, None, "pretty")
            printed = mock_print.call_args[0][0]
            assert "Login successful" in printed

    def test_write_to_file(self, tmp_path):
        payload = ok("login", "default", {"school": "bj80"})
        output_file = str(tmp_path / "output.json")
        print_payload(payload, output_file, "json")
        content = (tmp_path / "output.json").read_text()
        parsed = json.loads(content)
        assert parsed["ok"] is True
