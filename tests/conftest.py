"""Shared fixtures for tahuti tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Global user-state isolation ──────────────────────────────────────────
#
# Running this suite used to be able to destroy the operator's live
# credentials. `test_main.TestMainLogout.test_logout` redirected
# MB_CRAWLER_CONFIG and MB_CRAWLER_SESSION into a tmp_path but not
# MB_CRAWLER_CREDS_PATH, so `cmd_logout`'s `resolve_creds_path()` call fell
# through to the real ~/.config/tahuti/creds.json and `clear_creds()` unlinked
# the operator's ManageBac password. A test run must never be able to reach
# the real ~/.config/tahuti/, so every test gets the isolation below whether it
# asks for it or not.
#
# Two kinds of path need redirecting, and the environment alone reaches only
# one of them:
#
# 1. Read at *call* time by `config.resolve_*_path()`. Setting the variable is
#    sufficient.
# 2. Bound at *import* time. `DEFAULT_CACHE_DIR`, `DEFAULT_SNAPSHOT_PATH`,
#    `DEFAULT_STATE_PATH`, `DEFAULT_PID_PATH`, `DEFAULT_LOG_PATH` and
#    `DEFAULT_DAEMON_PATH` are module-level constants computed from
#    `config_dir()` when their module is first imported. No environment
#    variable can reach them, so each one is patched in place. Note that
#    `daemon/__init__.py` does `from .system import DEFAULT_PID_PATH`, which
#    creates a *second*, independent binding for the same value — both
#    namespaces are listed, or half the daemon paths would stay pointed at the
#    operator's home directory.
#
# The redirect target keeps the real on-disk layout (`~/.config/tahuti/...`)
# and `HOME` is pointed at the same tmp_path, so tests that legitimately
# assert "this default lives under the user's config directory" keep passing
# against the sandbox instead of having to be weakened into tautologies.

# Captured at import time, before any fixture can redirect it. Read-only: this
# is the location the tests below must prove they never touch, so it is
# deliberately never stat'd, written, or unlinked.
_REAL_HOME = Path.home()
REAL_CONFIG_DIR = _REAL_HOME / ".config" / "tahuti"

# (env var, filename under the redirected config dir) — read at call time.
_CALL_TIME_PATH_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("MANAGEBAC_CONFIG", "config.json"),
    ("MANAGEBAC_SESSION", "session.json"),
    ("MANAGEBAC_CREDS_PATH", "creds.json"),
)

# Credential/behaviour switches that a developer's shell may leak into the
# suite. Cleared rather than set, so tests that want one can opt back in. Both
# spellings, because the pre-rename `MB_CRAWLER_*` names still work as
# deprecated fallbacks — a leaked one would reach the code just as well as a
# leaked new one.
_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "MANAGEBAC_PASSWORD",
    "MANAGEBAC_COOKIE",
    "MANAGEBAC_KEYCHAIN",
    "MANAGEBAC_NO_PERM_WARN",
    "MB_CRAWLER_PASSWORD",
    "MB_CRAWLER_COOKIE",
    "MB_CRAWLER_KEYCHAIN",
    "MB_CRAWLER_NO_PERM_WARN",
)


def _redirected_paths(config_dir: Path) -> dict[str, Path]:
    """Every import-time path constant, mapped to its sandboxed value."""
    return {
        "mb_cli.config.CONFIG_DIR": config_dir,
        "mb_cli.config.DEFAULT_CONFIG_PATH": config_dir / "config.json",
        "mb_cli.config.DEFAULT_SESSION_PATH": config_dir / "session.json",
        "mb_cli.config.DEFAULT_CREDS_PATH": config_dir / "creds.json",
        "mb_cli.cache.DEFAULT_CACHE_DIR": config_dir / "cache",
        "mb_cli.__main__.DEFAULT_SNAPSHOT_PATH": config_dir / "snapshot.json",
        "mb_cli.daemon.DEFAULT_DAEMON_PATH": config_dir / "daemon.json",
        "mb_cli.daemon.DEFAULT_SNAPSHOT_PATH": config_dir / "snapshot.json",
        "mb_cli.daemon.state.DEFAULT_STATE_PATH": config_dir / "daemon_state.json",
        "mb_cli.daemon.system.DEFAULT_PID_PATH": config_dir / "daemon.pid",
        "mb_cli.daemon.system.DEFAULT_LOG_PATH": config_dir / "daemon.log",
        # `daemon/__init__.py` re-exports these two under its own names.
        "mb_cli.daemon.DEFAULT_PID_PATH": config_dir / "daemon.pid",
        "mb_cli.daemon.DEFAULT_LOG_PATH": config_dir / "daemon.log",
    }


@pytest.fixture()
def real_user_config_dir() -> Path:
    """The operator's real config dir, for asserting a test never touched it."""
    return REAL_CONFIG_DIR


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path: Path, monkeypatch):
    """Sandbox every path this package persists to, for every test.

    Autouse and unconditional so no test can forget it and no test can opt out
    by omission. A test that *wants* a different location can still have one:
    its own ``monkeypatch.setenv`` / ``setattr`` runs after this fixture, so a
    per-test override always wins over the sandbox default.
    """
    config_dir = tmp_path / ".config" / "tahuti"
    config_dir.mkdir(parents=True, exist_ok=True)

    for var, filename in _CALL_TIME_PATH_ENV_VARS:
        monkeypatch.setenv(var, str(config_dir / filename))
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    for target, value in _redirected_paths(config_dir).items():
        # Dotted-string form: imports the module on demand and fails loudly if
        # a constant is ever renamed, instead of silently leaving it live.
        monkeypatch.setattr(target, value)

    # Redirecting HOME catches the `Path.home()` calls that no constant covers
    # — the launchd plist and the systemd user unit in daemon/system.py — and
    # keeps `Path.home() / ".config" / "tahuti"` agreeing with the constants
    # above. Windows resolves the profile from these instead of HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # No test may reach a real OS credential store. `keychain.available()` is
    # the fallback `_load_creds()` consults when creds.json is missing, so a
    # helper that happens to be installed on the dev box would otherwise be
    # queried for the operator's password.
    monkeypatch.setattr("mb_cli.keychain._tool", lambda: None)

    yield config_dir


@pytest.fixture()
def tmp_config_dir(tmp_path: Path):
    """Return a temporary config directory."""
    return tmp_path / "config"


@pytest.fixture()
def sample_task_tile_html():
    """Return HTML for a single task tile."""
    return """
    <div class="f-task-tile">
      <a class="f-tile__title-link" href="/student/classes/1000023/core_tasks/1000026">Homework 3</a>
      <div class="f-tile__description">
        <span>Apr 15</span>
        <a href="/student/classes/1000023">Math HL</a>
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
      <a class="f-tile__title-link" href="/student/classes/1000023/core_tasks/1000027">Essay Draft</a>
      <div class="f-tile__description">
        <span>Apr 20</span>
        <a href="/student/classes/1000023">English A</a>
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
        <a href="/student/classes/1000023/attachments/12345/homework.pdf" class="fr-file">homework.pdf</a>
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
              <a class="f-timetable-item" data-bs-content-url="/student/ib_class_id=1000023">
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
      <form id="edit_dropbox_123" action="/student/classes/1000001/core_tasks/1000099/dropbox" method="post">
        <input type="file" name="dropbox[assets_attributes][0][file]">
      </form>
      <table>
        <tr><a href="/student/classes/1000023/attachments/99999/document.pdf">document.pdf</a></tr>
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
        <h4 class="title"><a href="/student/classes/1000001/core_tasks/1000099">Homework 1</a></h4>
        <span class="grade">A</span>
        <div class="points">95/100</div>
        <span class="submitted">Submitted</span>
        <div class="labels-set">
          <div class="label">Homework</div>
        </div>
      </div>
      <div class="fusion-card-item">
        <h4 class="title"><a href="/student/classes/1000001/core_tasks/1000099">Essay</a></h4>
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
        school="myschool",
        base_url="https://myschool.managebac.cn",
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
        "id": "1000026",
        "title": "Homework 3",
        "link": "https://myschool.managebac.cn/student/classes/1000023/core_tasks/1000026",
        "due_date": "Apr 15",
        "class_name": "Math HL",
        "labels": ["Homework"],
        "grade_letter": "A",
        "grade_score": "95/100",
        "view": "upcoming",
    }
