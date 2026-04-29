"""Tests for mb_crawler.client."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rm

from mb_crawler.cache import ResponseCache
from mb_crawler.client import HEADERS, ManageBacClient


@pytest.fixture()
def client(tmp_path: Path):
    """Create a ManageBacClient with a temp cache directory."""
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    c = ManageBacClient(
        "bj80",
        domain="managebac.cn",
        cache=cache,
        verify=False,
        retry=0,
    )
    c.set_cookie("test_session_cookie")
    return c


class TestManageBacClientInit:
    def test_base_url(self, client):
        assert client.base == "https://bj80.managebac.cn"

    def test_school_strips_domain(self):
        c = ManageBacClient("bj80.managebac.cn", domain="managebac.cn")
        assert c.school == "bj80"

    def test_default_domain(self):
        c = ManageBacClient("bj80")
        assert c.domain == "managebac.com"
        assert c.base == "https://bj80.managebac.com"

    def test_headers_set(self, client):
        for key, val in HEADERS.items():
            assert client.session.headers.get(key) == val

    def test_initial_student_name_none(self, client):
        assert client.student_name is None


class TestSetCookie:
    def test_sets_cookie(self, client):
        client.set_cookie("my_cookie")
        assert client.session.cookies.get("_managebac_session") == "my_cookie"

    def test_cookie_domain(self, client):
        client.set_cookie("c")
        cookies_dict = client.session.cookies.get_dict()
        assert "_managebac_session" in cookies_dict


class TestInvalidateCache:
    def test_invalidate(self, client):
        client.cache.put("https://example.com", "body", 200)
        client.invalidate_cache()
        assert client.cache.get("https://example.com") is None


class TestLogin:
    def test_login_success(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/login",
                text='<html><input name="authenticity_token" value="tok123"></html>',
            )
            m.post(
                "https://bj80.managebac.cn/sessions",
                status_code=302,
                headers={
                    "Location": "https://bj80.managebac.cn/student/tasks_and_deadlines"
                },
            )
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines",
                text="<html>Dashboard</html>",
            )
            assert client.login("user@example.com", "pass") is True

    def test_login_failure_wrong_credentials(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/login",
                text='<html><input name="authenticity_token" value="tok"></html>',
            )
            m.post(
                "https://bj80.managebac.cn/sessions",
                status_code=302,
                headers={"Location": "https://bj80.managebac.cn/login"},
            )
            assert client.login("user@example.com", "wrong") is False

    def test_login_no_csrf_token(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/login",
                text="<html><body>No form here</body></html>",
            )
            assert client.login("user@example.com", "pass") is False


class TestGetTasksByView:
    def test_single_page(self, client, sample_tasks_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://bj80\.managebac\.cn/student/tasks_and_deadlines"),
                text=sample_tasks_page_html,
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=1)
            assert len(tasks) == 2
            assert tasks[0]["title"] == "Homework 3"
            assert tasks[0]["view"] == "upcoming"
            assert tasks[0]["id"] == "27254393"
            assert tasks[0]["class_name"] == "Math HL"
            assert tasks[0]["grade_letter"] == "A"
            assert tasks[0]["grade_score"] == "95/100"
            assert tasks[1]["grade_letter"] is None

    def test_empty_page_stops(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://bj80\.managebac\.cn/student/tasks_and_deadlines"),
                text="<html><body>No tasks</body></html>",
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=10)
            assert tasks == []

    def test_pagination(
        self, client, sample_tasks_page_html, sample_tasks_page_html_no_next
    ):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"page=1"),
                text=sample_tasks_page_html,
            )
            m.get(
                re.compile(r"page=2"),
                text=sample_tasks_page_html_no_next,
            )
            tasks = client.get_tasks_by_view("upcoming", max_pages=10)
            assert len(tasks) == 3  # 2 from page 1 + 1 from page 2


class TestParseTile:
    def test_basic_tile(self, client, sample_task_tile_html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_task_tile_html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)
        assert result["title"] == "Homework 3"
        assert result["id"] == "27254393"
        assert result["due_date"] == "Apr 15"
        assert result["class_name"] == "Math HL"
        assert result["grade_letter"] == "A"
        assert result["grade_score"] == "95/100"

    def test_tile_no_link(self, client):
        from bs4 import BeautifulSoup

        html = '<div class="f-task-tile"><div>No link here</div></div>'
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        assert client._parse_tile(tile) is None

    def test_tile_labels(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div class="f-task-tile">
          <a class="f-tile__title-link" href="/student/classes/1/c/123">Test</a>
          <span class="badge">Urgent</span>
          <span class="badge">Homework</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find("div", class_="f-task-tile")
        result = client._parse_tile(tile)
        assert result["labels"] == ["Urgent", "Homework"]


class TestHasNextPage:
    def test_has_next_page(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><a href="?view=upcoming&page=2">Next</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is True

    def test_no_next_page(self, client, sample_tasks_page_html_no_next):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_tasks_page_html_no_next, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is False

    def test_next_page_button(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><button class="next" aria-label="Next page">></button></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is True

    def test_disabled_next_button(self, client):
        from bs4 import BeautifulSoup

        html = '<html><body><button class="next disabled" disabled>></button></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._has_next_page(soup, 1, "upcoming") is False


class TestCaptureStudentName:
    def test_captures_name(self, client, sample_tasks_page_html):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sample_tasks_page_html, "html.parser")
        client._capture_student_name(soup)
        assert client.student_name == "John Smith"

    def test_does_not_overwrite(self, client, sample_tasks_page_html):
        from bs4 import BeautifulSoup

        client.student_name = "Already Set"
        soup = BeautifulSoup(sample_tasks_page_html, "html.parser")
        client._capture_student_name(soup)
        assert client.student_name == "Already Set"

    def test_no_profile_link(self, client):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>Nothing</body></html>", "html.parser")
        client._capture_student_name(soup)
        assert client.student_name is None


class TestGetTaskDetail:
    def test_returns_description(self, client, sample_task_detail_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(
                    r"https://bj80\.managebac\.cn/student/classes/.+/core_tasks/.+"
                ),
                text=sample_task_detail_html,
            )
            detail = client.get_task_detail(
                "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393"
            )
            assert detail is not None
            assert "Complete the exercises" in detail["description"]
            assert detail["comments"][0] == "Teacher comment: Great work!"
            assert "Submitted" in detail["submission"]
            assert len(detail["attachments"]) >= 1

    def test_strips_base_url(self, client, sample_task_detail_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(
                    r"https://bj80\.managebac\.cn/student/classes/.+/core_tasks/.+"
                ),
                text=sample_task_detail_html,
            )
            detail = client.get_task_detail(
                "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393"
            )
            assert detail is not None


class TestCrawlAll:
    def test_combines_views(self, client, sample_tasks_page_html_no_next):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"view=upcoming"),
                text=sample_tasks_page_html_no_next,
            )
            m.get(
                re.compile(r"view=past"),
                text="<html><body>No tasks</body></html>",
            )
            m.get(
                re.compile(r"view=overdue"),
                text="<html><body>No tasks</body></html>",
            )
            result = client.crawl_all(max_pages=1, fetch_details=False)
            assert result["school"] == "bj80"
            assert result["base_url"] == "https://bj80.managebac.cn"
            assert len(result["upcoming"]) == 1
            assert len(result["past"]) == 0
            assert len(result["overdue"]) == 0
            assert result["summary"]["upcoming_count"] == 1
            assert result["crawled_at"] is not None

    def test_with_fetch_details(
        self, client, sample_tasks_page_html_no_next, sample_task_detail_html
    ):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"view=upcoming"),
                text=sample_tasks_page_html_no_next,
            )
            m.get(
                re.compile(r"view=past"),
                text="<html></html>",
            )
            m.get(
                re.compile(r"view=overdue"),
                text="<html></html>",
            )
            m.get(
                re.compile(r"/student/classes/\d+/core_tasks/\d+$"),
                text=sample_task_detail_html,
            )
            result = client.crawl_all(max_pages=1, fetch_details=True)
            assert len(result["upcoming"]) == 1
            assert "detail" in result["upcoming"][0]


class TestGetCalendarEvents:
    def test_returns_events(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/events.json",
                json=[
                    {
                        "id": 1,
                        "title": "Exam",
                        "start": "2026-04-30T09:00:00",
                        "end": "2026-04-30T12:00:00",
                        "allDay": False,
                        "description": "<p>Math exam</p>",
                        "type": "exam",
                        "category": "assessment",
                        "url": "/student/calendar/1",
                        "backgroundColor": "#ff0000",
                    }
                ],
            )
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert len(events) == 1
            assert events[0]["title"] == "Exam"
            assert events[0]["all_day"] is False
            assert events[0]["color"] == "#ff0000"

    def test_empty_events(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/events.json",
                json=[],
            )
            events = client.get_calendar_events("2026-04-29", "2026-05-05")
            assert events == []


class TestGetICalFeed:
    def test_fetches_ical(self, client, sample_calendar_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/calendar",
                text=sample_calendar_page_html,
            )
            m.get(
                "https://managebac.com/student/events/token/abc123.ics",
                text="BEGIN:VCALENDAR\nEND:VCALENDAR",
            )
            ical = client.get_ical_feed()
            assert "VCALENDAR" in ical

    def test_no_webcal_link_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/calendar",
                text="<html><body>No link</body></html>",
            )
            with pytest.raises(RuntimeError, match="webcal"):
                client.get_ical_feed()


class TestGetTimetable:
    def test_parses_timetable(self, client, sample_timetable_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://bj80\.managebac\.cn/student/timetables"),
                text=sample_timetable_html,
            )
            result = client.get_timetable("2026-04-28")
            assert len(result["days"]) == 2
            assert result["days"][0]["header"] == "Monday"
            assert result["days"][0]["is_today"] is True
            assert len(result["lessons"]) == 1
            lesson = result["lessons"][0]
            assert lesson["subject"] == "Math HL"
            assert lesson["period"] == "P1"
            assert lesson["teacher"] == "Mr. Smith"
            assert lesson["room"] == "Room 101"
            assert lesson["class_id"] == "11460711"

    def test_no_table_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"https://bj80\.managebac\.cn/student/timetables"),
                text="<html><body>No timetable</body></html>",
            )
            with pytest.raises(RuntimeError, match="timetable"):
                client.get_timetable()


class TestGetClassGrades:
    def test_parses_grades(self, client, sample_grades_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/classes/11460711/core_tasks",
                text=sample_grades_page_html,
            )
            result = client.get_class_grades("11460711")
            assert len(result["tasks"]) == 2
            assert result["tasks"][0]["grade_letter"] == "A"
            assert result["tasks"][0]["category"] == "Homework"
            assert len(result["categories"]) == 2
            assert result["categories"][0]["name"] == "Homework"
            assert result["categories"][0]["weight"] == 0.4
            assert result["expected_grade"] is not None
            assert result["expected_grade"]["letter_grade"] == "B"

    def test_no_grades_page(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/classes/999/core_tasks",
                text="<html><body>No grades</body></html>",
            )
            result = client.get_class_grades("999")
            assert result["tasks"] == []
            assert result["categories"] == []


class TestSubmitFile:
    def test_submit_success(self, client, sample_dropbox_page_html, tmp_path: Path):
        test_file = tmp_path / "homework.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/11460711/core_tasks/27254393/dropbox$"),
                text=sample_dropbox_page_html,
            )
            m.post(
                "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393/dropbox/upload",
                json={"ok": True},
            )
            result = client.submit_file("11460711", "27254393", str(test_file))
            assert result["ok"] is True
            assert result["filename"] == "homework.pdf"
            assert "27254393" in result["task_url"]

    def test_submit_file_not_found(self, client, sample_dropbox_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=sample_dropbox_page_html,
            )
            with pytest.raises(FileNotFoundError):
                client.submit_file("11460711", "27254393", "/nonexistent/file.pdf")

    def test_submit_no_csrf_raises(self, client, tmp_path: Path):
        test_file = tmp_path / "hw.pdf"
        test_file.write_bytes(b"content")
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text="<html><body>No CSRF</body></html>",
            )
            with pytest.raises(RuntimeError, match="CSRF"):
                client.submit_file("11460711", "27254393", str(test_file))

    def test_submit_no_form_raises(self, client, tmp_path: Path):
        test_file = tmp_path / "hw.pdf"
        test_file.write_bytes(b"content")
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text='<html><head><meta name="csrf-token" content="tok"></head><body>No form</body></html>',
            )
            with pytest.raises(RuntimeError, match="form"):
                client.submit_file("11460711", "27254393", str(test_file))


class TestGetSubmissions:
    def test_returns_submissions(self, client, sample_dropbox_page_html):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=sample_dropbox_page_html,
            )
            subs = client.get_submissions("11460711", "27254393")
            assert len(subs) == 1
            assert subs[0]["name"] == "document.pdf"

    def test_filters_view_feedback(self, client):
        html = """
        <html><body>
        <table>
          <tr><a href="/student/classes/1/attachments/1/file.pdf">file.pdf</a></tr>
          <tr><a href="/student/classes/1/attachments/2/feedback.pdf">View Teacher Feedback</a></tr>
        </table>
        </body></html>
        """
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=html,
            )
            subs = client.get_submissions("1", "1")
            assert len(subs) == 1
            assert subs[0]["name"] == "file.pdf"

    def test_error_returns_list_with_error(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                status_code=500,
            )
            subs = client.get_submissions("1", "1")
            assert len(subs) == 1
            assert "error" in subs[0]


class TestExtractAttachments:
    def test_extracts_file_links(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div>
          <a href="/student/classes/1/attachments/123/report.pdf" class="fr-file">report.pdf</a>
          <a href="javascript:void(0)">ignore</a>
          <a href="mailto:test@test.com">ignore</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = client._extract_attachments(soup)
        assert len(attachments) == 1
        assert attachments[0]["name"] == "report.pdf"
        assert "report.pdf" in attachments[0]["url"]

    def test_deduplicates(self, client):
        from bs4 import BeautifulSoup

        html = """
        <div>
          <a href="/student/classes/1/attachments/123/file.pdf">file.pdf</a>
          <a href="/student/classes/1/attachments/123/file.pdf">file.pdf</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = client._extract_attachments(soup)
        assert len(attachments) == 1


class TestGetNotificationToken:
    def test_extracts_token(self, client, sample_notifications_page_html):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/notifications",
                text=sample_notifications_page_html,
            )
            endpoint, token = client.get_notification_token()
            assert endpoint == "https://mnn-hub.prod.faria.com"
            assert token == "eyJhbGciOiJIUzI1NiJ9.test.token"

    def test_no_trigger_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/notifications",
                text="<html><body>No trigger</body></html>",
            )
            with pytest.raises(RuntimeError, match="trigger"):
                client.get_notification_token()


class TestGetCsrf:
    def test_extracts_csrf(self, client):
        from bs4 import BeautifulSoup

        html = '<html><head><meta name="csrf-token" content="abc123"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        assert client._get_csrf(soup) == "abc123"

    def test_no_csrf_returns_none(self, client):
        from bs4 import BeautifulSoup

        html = "<html><head></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        assert client._get_csrf(soup) is None


class TestCountGradeFrequencies:
    def test_counts_across_classes(self, client):
        tasks_data = {
            "upcoming": [
                {
                    "id": "1",
                    "class_name": "Math",
                    "link": "/student/classes/100/core_tasks/1",
                },
            ],
            "past": [],
            "overdue": [],
        }
        grades_data = {
            "tasks": [
                {"grade_letter": "A"},
                {"grade_letter": "B"},
                {"grade_letter": "A"},
            ],
            "categories": [],
            "grade_scale": {},
            "expected_grade": None,
        }
        with rm.Mocker() as m:
            m.get(re.compile(r"view=upcoming"), json=[])
            m.get(re.compile(r"view=past"), json=[])
            m.get(re.compile(r"view=overdue"), json=[])
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                text="<html></html>",
            )
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines?view=past&page=1",
                text="<html></html>",
            )
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines?view=overdue&page=1",
                text="<html></html>",
            )
            # We need to mock crawl_all and get_class_grades
            with patch.object(
                client,
                "crawl_all",
                return_value={
                    "upcoming": [
                        {
                            "id": "1",
                            "class_name": "Math",
                            "link": "/student/classes/100/core_tasks/1",
                        }
                    ],
                    "past": [],
                    "overdue": [],
                },
            ):
                with patch.object(
                    client,
                    "get_class_grades",
                    return_value={
                        "tasks": [
                            {"grade_letter": "A"},
                            {"grade_letter": "B"},
                            {"grade_letter": "A"},
                        ],
                    },
                ):
                    result = client.count_grade_frequencies()
                    assert result["grades"] == {"A": 2, "B": 1}
                    assert result["total"] == 3

    def test_filter_no_match(self, client):
        with patch.object(
            client,
            "crawl_all",
            return_value={
                "upcoming": [
                    {
                        "id": "1",
                        "class_name": "Math",
                        "link": "/student/classes/100/core_tasks/1",
                    }
                ],
                "past": [],
                "overdue": [],
            },
        ):
            result = client.count_grade_frequencies(class_filter="Physics")
            assert "error" in result
            assert "Physics" in result["error"]


class TestRetryLogic:
    def test_retries_on_connection_error(self, tmp_path: Path):
        import requests as _requests

        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("bj80", domain="managebac.cn", cache=cache, retry=2)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/login",
                [
                    {"exc": _requests.ConnectionError("fail")},
                    {"exc": _requests.ConnectionError("fail")},
                    {
                        "text": '<html><input name="authenticity_token" value="t"></html>'
                    },
                ],
            )
            m.post(
                "https://bj80.managebac.cn/sessions",
                status_code=302,
                headers={"Location": "/student/tasks_and_deadlines"},
            )
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines",
                text="<html>Dashboard</html>",
            )
            with patch("mb_crawler.client.time.sleep"):
                assert client.login("a@b.com", "pass") is True

    def test_retries_on_503(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("bj80", domain="managebac.cn", cache=cache, retry=1)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                [
                    {"status_code": 503, "text": "Service Unavailable"},
                    {"text": "<html></html>"},
                ],
            )
            with patch("mb_crawler.client.time.sleep"):
                tasks = client.get_tasks_by_view("upcoming", max_pages=1)
                assert tasks == []

    def test_max_retries_exceeded(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=False)
        client = ManageBacClient("bj80", domain="managebac.cn", cache=cache, retry=1)
        client.set_cookie("c")
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
                exc=ConnectionError("fail"),
            )
            with patch("mb_crawler.client.time.sleep"):
                with pytest.raises(ConnectionError):
                    client.get_tasks_by_view("upcoming", max_pages=1)


class TestGetCached:
    def test_returns_cached_response(self, client, sample_tasks_page_html):
        client.cache.put(
            "https://bj80.managebac.cn/student/tasks_and_deadlines?view=upcoming&page=1",
            sample_tasks_page_html,
            200,
        )
        soup = client._get("/student/tasks_and_deadlines?view=upcoming&page=1")
        assert soup.find("title") is not None

    def test_session_expired_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                "https://bj80.managebac.cn/student/tasks",
                status_code=302,
                headers={"Location": "/login"},
            )
            m.get(
                "https://bj80.managebac.cn/login",
                text="<html>login page</html>",
            )
            with pytest.raises(RuntimeError, match="expired"):
                client._get("/student/tasks")
