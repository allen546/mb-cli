"""Shared fixtures for mb-cli tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def tmp_config_dir(tmp_path: Path):
    """Return a temporary config directory."""
    return tmp_path / "config"


@pytest.fixture()
def sample_task_tile_html():
    """Return HTML for a single task tile."""
    return """
    <div class="f-task-tile">
      <a class="f-tile__title-link" href="/student/classes/11460711/core_tasks/27254393">Homework 3</a>
      <div class="f-tile__description">
        <span>Apr 15</span>
        <a href="/student/classes/11460711">Math HL</a>
      </div>
      <div class="f-tile__suffix">
        <div class="f-task-score">
          <h4>A</h4>
          <p>95/100</p>
        </div>
      </div>
    </div>
    """


@pytest.fixture()
def sample_task_tile_html_no_grade():
    """Return HTML for a task tile without a grade."""
    return """
    <div class="f-task-tile">
      <a class="f-tile__title-link" href="/student/classes/11460711/core_tasks/27254394">Essay Draft</a>
      <div class="f-tile__description">
        <span>Apr 20</span>
        <a href="/student/classes/11460711">English A</a>
      </div>
    </div>
    """


@pytest.fixture()
def sample_tasks_page_html(sample_task_tile_html, sample_task_tile_html_no_grade):
    """Return HTML for a tasks listing page."""
    return f"""
    <html>
    <head><title>Tasks</title></head>
    <body>
      <a href="/student/profile">John Smith — ManageBac</a>
      <div>{sample_task_tile_html}</div>
      <div>{sample_task_tile_html_no_grade}</div>
      <a href="?view=upcoming&page=2">Next</a>
    </body>
    </html>
    """


@pytest.fixture()
def sample_tasks_page_html_no_next(sample_task_tile_html):
    """Return HTML for a tasks listing page with no next page."""
    return f"""
    <html>
    <body>
      <div>{sample_task_tile_html}</div>
    </body>
    </html>
    """


@pytest.fixture()
def sample_task_detail_html():
    """Return HTML for a task detail page."""
    return """
    <html>
    <head><meta name="csrf-token" content="abc123csrf"></head>
    <body>
      <main>
        <h3>Description</h3>
        <div class="fr-view">Complete the exercises from chapter 5.</div>
        <div class="discussion">
          <div class="fr-view">Teacher comment: Great work!</div>
        </div>
        <div class="dropbox">
          <p>Submitted: 1 file</p>
        </div>
        <a href="/student/classes/11460711/attachments/12345/homework.pdf" class="fr-file">homework.pdf</a>
      </main>
    </body>
    </html>
    """


@pytest.fixture()
def sample_login_page_html():
    """Return HTML for the login page."""
    return """
    <html>
    <body>
      <form>
        <input name="authenticity_token" value="csrf_token_abc123">
      </form>
    </body>
    </html>
    """


@pytest.fixture()
def sample_notifications_page_html():
    """Return HTML for the notifications page."""
    return """
    <html>
    <body>
      <a class="js-messages-and-notifications-trigger"
         data-token="eyJhbGciOiJIUzI1NiJ9.test.token"
         data-mnn-hub-endpoint="https://mnn-hub.prod.faria.com">
      </a>
    </body>
    </html>
    """


@pytest.fixture()
def sample_calendar_page_html():
    """Return HTML for the calendar page with webcal link."""
    return """
    <html>
    <body>
      <a href="webcal://managebac.com/student/events/token/abc123.ics">Subscribe</a>
    </body>
    </html>
    """


@pytest.fixture()
def sample_timetable_html():
    """Return HTML for the timetable page."""
    return """
    <html>
    <body>
      <table class="f-timetable">
        <thead>
          <tr>
            <th>Period</th>
            <th class="table-active-th">Monday</th>
            <th>Tuesday</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th>P1</th>
            <td>
              <a class="f-timetable-item" data-bs-content-url="/student/ib_class_id=11460711">
                <div class="f-box-item__body">
                  <small class="color-secondary">08:00 - 08:45</small>
                  <p class="fw-semibold">Math HL</p>
                  <p class="text-truncate">Year 11</p>
                  <p class="text-truncate">Mr. Smith</p>
                  <p>Room 101</p>
                </div>
              </a>
            </td>
            <td></td>
          </tr>
        </tbody>
      </table>
    </body>
    </html>
    """


@pytest.fixture()
def sample_dropbox_page_html():
    """Return HTML for the dropbox upload page."""
    return """
    <html>
    <head><meta name="csrf-token" content="csrf_token_xyz789"></head>
    <body>
      <form id="edit_dropbox_123" action="/student/classes/11460711/core_tasks/27254393/dropbox" method="post">
        <input type="file" name="dropbox[assets_attributes][0][file]">
      </form>
      <table>
        <tr><a href="/student/classes/11460711/attachments/99999/document.pdf">document.pdf</a></tr>
      </table>
    </body>
    </html>
    """


@pytest.fixture()
def sample_grades_page_html():
    """Return HTML for the class grades page."""
    return """
    <html>
    <body>
      <div class="assignments-progress-chart"
           data-grade-labels='{"0":"F","1":"E","2":"D","3":"C","4":"B","5":"A"}'
           data-series='[{"name":"Homework 1","data":[4]},{"name":"Essay","data":[5]}]'>
      </div>
      <div id="categories-table">
        <div class="list-item">
          <div class="cell">Category</div>
          <div class="cell">Weight</div>
        </div>
        <div class="list-item">
          <div class="cell">Homework</div>
          <div class="cell">40%</div>
        </div>
        <div class="list-item">
          <div class="cell">Exam</div>
          <div class="cell">60%</div>
        </div>
      </div>
      <div class="fusion-card-item">
        <h4 class="title"><a href="/student/classes/11460711/core_tasks/1001">Homework 1</a></h4>
        <span class="grade">A</span>
        <div class="points">95/100</div>
        <span class="submitted">Submitted</span>
        <div class="labels-set">
          <div class="label">Homework</div>
        </div>
      </div>
      <div class="fusion-card-item">
        <h4 class="title"><a href="/student/classes/11460711/core_tasks/1002">Essay</a></h4>
        <span class="grade">B+</span>
        <div class="points">88/100</div>
        <span class="not-submitted">Not Submitted</span>
        <div class="labels-set">
          <div class="label">Exam</div>
        </div>
      </div>
    </body>
    </html>
    """


@pytest.fixture()
def make_crawl_result():
    """Factory fixture that creates a crawl_all()-style result dict."""

    def _make(
        upcoming=None,
        past=None,
        overdue=None,
        student_name="Test Student",
        school="bj80",
        base_url="https://bj80.managebac.cn",
    ):
        upcoming = upcoming or []
        past = past or []
        overdue = overdue or []
        return {
            "student_name": student_name,
            "school": school,
            "base_url": base_url,
            "crawled_at": "2026-04-29T12:00:00",
            "upcoming": upcoming,
            "past": past,
            "overdue": overdue,
            "summary": {
                "upcoming_count": len(upcoming),
                "past_count": len(past),
                "overdue_count": len(overdue),
            },
        }

    return _make


@pytest.fixture()
def sample_task():
    """Return a sample task dict."""
    return {
        "id": "27254393",
        "title": "Homework 3",
        "link": "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393",
        "due_date": "Apr 15",
        "class_name": "Math HL",
        "labels": ["Homework"],
        "grade_letter": "A",
        "grade_score": "95/100",
        "view": "upcoming",
    }
