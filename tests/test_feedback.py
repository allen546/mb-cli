"""Tests for teacher feedback fetching feature."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from mb_cli.client import ManageBacClient
from mb_cli.daemon import diff_index


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_client() -> ManageBacClient:
    import threading
    from mb_cli.cache import ResponseCache
    client = ManageBacClient.__new__(ManageBacClient)
    client.school = "testschool"
    client.domain = "managebac.cn"
    client.base = "https://testschool.managebac.cn"
    client.student_name = "Test Student"
    client.cache = ResponseCache()
    client.retry = 0
    client.request_delay = 0.0
    client._last_request_time = 0.0
    client._last_url = None
    client._url_locks = {}
    client._url_locks_mutex = threading.Lock()
    client.session = MagicMock()
    return client


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ── get_submissions: feedback_url capture ────────────────────────────────


DROPBOX_WITH_FEEDBACK = """
<table>
  <tr>
    <td>
      <a href="/attachments/12345/essay.pdf">essay.pdf</a>
      <a href="/student/classes/111/core_tasks/222/teacher_evaluations/999">View teacher feedback</a>
    </td>
  </tr>
  <tr>
    <td>
      <a href="/attachments/99999/notes.docx">notes.docx</a>
    </td>
  </tr>
</table>
"""

DROPBOX_NO_FEEDBACK = """
<table>
  <tr>
    <td><a href="/attachments/54321/hw.pdf">hw.pdf</a></td>
  </tr>
</table>
"""


def test_get_submissions_captures_feedback_url():
    client = _make_client()
    with patch.object(client, "_get", return_value=_soup(DROPBOX_WITH_FEEDBACK)):
        subs = client.get_submissions("111", "222")

    assert len(subs) == 2
    assert subs[0]["name"] == "essay.pdf"
    assert "feedback_url" in subs[0]
    assert subs[0]["feedback_url"].endswith("/teacher_evaluations/999")
    assert subs[1]["name"] == "notes.docx"
    assert "feedback_url" not in subs[1]


def test_get_submissions_no_feedback_url():
    client = _make_client()
    with patch.object(client, "_get", return_value=_soup(DROPBOX_NO_FEEDBACK)):
        subs = client.get_submissions("111", "222")

    assert len(subs) == 1
    assert subs[0]["name"] == "hw.pdf"
    assert "feedback_url" not in subs[0]


# ── _parse_feedback_page ─────────────────────────────────────────────────


FEEDBACK_PAGE_COMMENT_ONLY = """
<html><body>
  <div class="fr-view">Great work! Well structured argument.</div>
</body></html>
"""

FEEDBACK_PAGE_WITH_RUBRIC = """
<html><body>
  <div class="fr-view">Good effort overall.</div>
  <table>
    <tr><th>Criterion</th><th>Score</th><th>Max</th></tr>
    <tr><td>Analysis</td><td>8</td><td>10</td></tr>
    <tr><td>Writing</td><td>7</td><td>10</td></tr>
  </table>
</body></html>
"""

FEEDBACK_PAGE_WITH_ATTACHMENT = """
<html><body>
  <div class="fr-view">See annotated PDF.</div>
  <a href="/attachments/77777/annotated.pdf">annotated.pdf</a>
</body></html>
"""


def test_parse_feedback_page_comment():
    client = _make_client()
    url = "https://testschool.managebac.cn/student/classes/111/core_tasks/222/teacher_evaluations/999"
    with patch.object(client, "_get", return_value=_soup(FEEDBACK_PAGE_COMMENT_ONLY)):
        result = client._parse_feedback_page(url)

    assert result["error"] is None
    assert "Great work" in result["comment"]
    assert result["rubric"] == []
    assert result["attachments"] == []


def test_parse_feedback_page_rubric():
    client = _make_client()
    url = "https://testschool.managebac.cn/student/classes/111/core_tasks/222/teacher_evaluations/999"
    with patch.object(client, "_get", return_value=_soup(FEEDBACK_PAGE_WITH_RUBRIC)):
        result = client._parse_feedback_page(url)

    assert result["error"] is None
    assert len(result["rubric"]) == 2
    assert result["rubric"][0] == {"criterion": "Analysis", "score": "8", "max": "10"}
    assert result["rubric"][1] == {"criterion": "Writing", "score": "7", "max": "10"}


def test_parse_feedback_page_attachments():
    client = _make_client()
    url = "https://testschool.managebac.cn/student/classes/111/core_tasks/222/teacher_evaluations/999"
    with patch.object(client, "_get", return_value=_soup(FEEDBACK_PAGE_WITH_ATTACHMENT)):
        result = client._parse_feedback_page(url)

    assert result["error"] is None
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["name"] == "annotated.pdf"
    assert result["attachments"][0]["url"].endswith("/attachments/77777/annotated.pdf")


def test_parse_feedback_page_fetch_error():
    client = _make_client()
    url = "https://testschool.managebac.cn/student/classes/111/core_tasks/222/teacher_evaluations/999"
    with patch.object(client, "_get", side_effect=RuntimeError("Session expired")):
        result = client._parse_feedback_page(url)

    assert result["comment"] is None
    assert result["rubric"] == []
    assert result["attachments"] == []
    assert "Session expired" in result["error"]


# ── get_teacher_feedback orchestration ───────────────────────────────────


def test_get_teacher_feedback_full_flow():
    client = _make_client()

    fake_submissions = [
        {
            "name": "essay.pdf",
            "url": "https://testschool.managebac.cn/attachments/12345/essay.pdf",
            "feedback_url": "https://testschool.managebac.cn/student/classes/111/core_tasks/222/teacher_evaluations/999",
        }
    ]
    fake_parsed = {
        "comment": "Well done!",
        "rubric": [{"criterion": "Analysis", "score": "9", "max": "10"}],
        "attachments": [],
        "error": None,
    }

    with (
        patch.object(client, "get_submissions", return_value=fake_submissions),
        patch.object(client, "_parse_feedback_page", return_value=fake_parsed),
    ):
        result = client.get_teacher_feedback("111", "222")

    assert "task_url" in result
    assert len(result["feedback_items"]) == 1
    item = result["feedback_items"][0]
    assert item["submission_name"] == "essay.pdf"
    assert item["comment"] == "Well done!"
    assert item["rubric"][0]["criterion"] == "Analysis"
    assert item["feedback_url"].endswith("/teacher_evaluations/999")


def test_get_teacher_feedback_no_feedback_url():
    client = _make_client()

    fake_submissions = [
        {
            "name": "hw.pdf",
            "url": "https://testschool.managebac.cn/attachments/54321/hw.pdf",
        }
    ]

    with (
        patch.object(client, "get_submissions", return_value=fake_submissions),
        patch.object(client, "_parse_feedback_page") as mock_parse,
    ):
        result = client.get_teacher_feedback("111", "222")

    mock_parse.assert_not_called()
    item = result["feedback_items"][0]
    assert item["comment"] is None
    assert item["rubric"] == []
    assert item["feedback_url"] is None


# ── daemon: new_feedback alert ───────────────────────────────────────────


def test_diff_index_new_feedback_alert():
    task_with_feedback = {
        "id": "222",
        "title": "AP Calc BC Task 1",
        "class_name": "AP Calc BC",
        "view": "past",
        "grade_letter": "A",
        "feedback_items": [
            {
                "submission_name": "essay.pdf",
                "comment": "Excellent!",
                "rubric": [],
                "attachments": [],
            }
        ],
    }

    old = {
        "upcoming": [],
        "past": [{"id": "222", "title": "AP Calc BC Task 1", "class_name": "AP Calc BC", "view": "past", "grade_letter": "A"}],
        "overdue": [],
    }
    new = {"upcoming": [], "past": [task_with_feedback], "overdue": []}

    alerts, changed_ids = diff_index(old, new)

    fb_alerts = [a for a in alerts if a["type"] == "new_feedback"]
    assert len(fb_alerts) == 1
    assert "essay.pdf" in fb_alerts[0]["message"]
    assert "AP Calc BC Task 1" in fb_alerts[0]["message"]
    assert "222" in changed_ids


def test_diff_index_no_feedback_alert_when_already_seen():
    task = {
        "id": "222",
        "title": "AP Calc BC Task 1",
        "class_name": "AP Calc BC",
        "view": "past",
        "grade_letter": "A",
        "feedback_items": [
            {
                "submission_name": "essay.pdf",
                "comment": "Excellent!",
                "rubric": [],
                "attachments": [],
            }
        ],
    }

    old = {
        "upcoming": [],
        "past": [task],
        "overdue": [],
        "feedback_seen": {"222": ["essay.pdf"]},
    }
    new = {"upcoming": [], "past": [task], "overdue": []}

    alerts, _ = diff_index(old, new)
    fb_alerts = [a for a in alerts if a["type"] == "new_feedback"]
    assert len(fb_alerts) == 0


def test_diff_index_no_feedback_alert_without_feedback_items():
    task = {
        "id": "333",
        "title": "Physics Quiz",
        "class_name": "Physics",
        "view": "past",
        "grade_letter": "B",
    }
    old = {"upcoming": [], "past": [], "overdue": []}
    new = {"upcoming": [], "past": [task], "overdue": []}

    alerts, _ = diff_index(old, new)
    fb_alerts = [a for a in alerts if a["type"] == "new_feedback"]
    assert len(fb_alerts) == 0
