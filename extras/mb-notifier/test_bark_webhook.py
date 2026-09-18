import hashlib
import hmac
import io
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests_mock

from bark_webhook_receiver import (
    DEFAULT_ALIASES_PATH,
    MAX_BODY_BYTES,
    MAX_COURSE_LEN,
    MAX_TASK_LEN,
    _log_safe,
    clean_task_title,
    compute_signature,
    format_event_for_bark,
    is_task_event_suppressed,
    load_course_aliases,
    make_request_handler,
    reset_replay_cache,
    resolve_course_name,
    sanitize_tap_url,
    signed_material,
    truncate,
    verify_signature,
)


def test_resolve_course_name_exact_match():
    aliases = {
        "English Language Arts Hons": "ELA Hons",
        "AP Computer Science A": "AP CSA",
    }
    # Exact match replaces with alias
    assert resolve_course_name("English Language Arts Hons", aliases) == "ELA Hons"
    assert resolve_course_name("AP Computer Science A", aliases) == "AP CSA"


def test_resolve_course_name_zero_autocleaning():
    aliases = {"English Language Arts Hons": "ELA Hons"}
    # ManageBac messy course name must NOT be autocleaned if not matched in aliases
    raw = "AP AP—Calculus BC 2025-2026 (Grade 10) CLASS 1 BLUE"
    assert resolve_course_name(raw, aliases) == truncate(raw, MAX_COURSE_LEN)

    # Case-sensitive: lowercase should NOT match
    assert resolve_course_name("english language arts hons", aliases) == "english language arts hons"

    # None or empty
    assert resolve_course_name(None, aliases) == "ManageBac"
    assert resolve_course_name("", aliases) == "ManageBac"


def test_load_course_aliases(tmp_path):
    # Non-existent file
    assert load_course_aliases(tmp_path / "non_existent.json") == {}

    # Valid JSON
    alias_file = tmp_path / "aliases.json"
    alias_file.write_text(json.dumps({"Math": "M", "Physics": "P"}), encoding="utf-8")
    loaded = load_course_aliases(alias_file)
    assert loaded == {"Math": "M", "Physics": "P"}

    # Invalid JSON
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    assert load_course_aliases(bad_file) == {}

    # Non-dict JSON
    list_file = tmp_path / "list.json"
    list_file.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert load_course_aliases(list_file) == {}


def test_clean_task_title():
    assert clean_task_title("New Task: Chapter 1 Questions") == "Chapter 1 Questions"
    assert clean_task_title("Updated Task: Math HW") == "Math HW"
    assert clean_task_title("Task: Essay Draft") == "Essay Draft"
    assert clean_task_title(None) == "未命名作业"

    # Long task title should allow up to MAX_TASK_LEN (80 chars)
    long_title = "A" * 70
    assert clean_task_title(long_title) == long_title
    too_long = "B" * 100
    res = clean_task_title(too_long)
    assert len(res) == MAX_TASK_LEN
    assert res.endswith("..")


def test_format_event_new_task_layout_and_no_teacher():
    payload = {
        "event": "task_created",
        "data": {
            "class_name": "English Language Arts Hons",
            "task_title": "Comprehension Questions Chapters 11 to 19 Analysis",
            "due_date": "2026-09-10T23:59:00",
            "sender": {"name": "张老师 | Zhang San (Alex)"},
            "class_id": "123",
            "task_id": "456",
        },
    }
    aliases = {"English Language Arts Hons": "ELA Hons"}
    title, message, sound, priority, url = format_event_for_bark(payload, aliases=aliases)

    assert title == "📝 New Task"
    assert sound == "bell"
    assert priority == 6
    assert url == "https://demo-school.managebac.cn/student/classes/123/core_tasks/456"

    # Message must NOT contain teacher info
    assert "张老师" not in message
    assert "Zhang San" not in message
    assert "教师" not in message

    # Message must have exactly 3 logical lines
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: ELA Hons"
    assert lines[1] == "作业: Comprehension Questions Chapters 11 to 19 Analysis"
    assert lines[2].startswith("截止: 09-10 23:59")


def test_format_event_ddl_warning():
    payload = {
        "event": "deadline_approaching",
        "data": {
            "class_name": "AP Calculus BC",
            "task_title": "Problem Set 4",
            "due_date": "2026-09-04T15:30:00",
            "time_remaining_minutes": 120,
            "sender": {"name": "Mr. Smith"},
        },
    }
    aliases = {"AP Calculus BC": "AP Calc BC"}
    title, message, sound, priority, _ = format_event_for_bark(payload, aliases=aliases)

    assert title == "⏰ DDL Warning"
    assert priority == 10
    assert sound == "alarm"
    assert "Mr. Smith" not in message
    assert "教师" not in message

    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: AP Calc BC"
    assert lines[1] == "作业: Problem Set 4"
    assert "截止: 09-04 15:30 (还剩 2小时)" in lines[2]


def test_format_event_ddl_warning_rollover():
    # 23 hours 59.8 minutes should roll over to 24小时, not 23小时60分
    payload = {
        "event": "deadline_approaching",
        "data": {
            "class_name": "Pre-AP Chemistry",
            "task_title": "Homework of summer holiday",
            "due_date": "2026-09-07T08:00:00",
            "time_remaining_minutes": 1439.8,
        },
    }
    _, message, _, _, _ = format_event_for_bark(payload)
    assert "(还剩 24小时)" in message
    assert "60分" not in message

    # 59.8 minutes should roll over to 仅剩 1小时, not 0小时60分
    payload["data"]["time_remaining_minutes"] = 59.8
    _, message2, _, _, _ = format_event_for_bark(payload)
    assert "(仅剩 1小时)" in message2


def test_format_event_task_updated_submitted_status():
    payload = {
        "event": "task_updated",
        "data": {
            "class_name": "Chemistry",
            "title": "Lab Report",
            "due_date": "2026-09-05T12:00:00",
            "enriched_task": {"status": "submitted"},
        },
    }
    title, message, _, _, _ = format_event_for_bark(payload)
    assert title == "✏️ Updated Task"
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: Chemistry"
    assert lines[1] == "作业: Lab Report"
    assert lines[2].endswith("(已提交)")


def test_format_event_grade_posted():
    payload = {
        "event": "grade_posted",
        "data": {
            "class_name": "Physics",
            "task_title": "Quiz 1",
            "grade_letter": "A",
            "points": "95/100",
        },
    }
    title, message, sound, priority, _ = format_event_for_bark(payload)
    assert title == "📊 Grade Posted"
    assert sound == "chime"
    assert priority == 7
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: Physics"
    assert lines[1] == "作业: Quiz 1"
    assert lines[2] == "得分: A 95/100"


def test_format_event_file_uploaded():
    payload = {
        "event": "file_uploaded",
        "data": {
            "class_name": "History",
            "body_preview": "Uploaded a new file named syllabus_2026.pdf for review",
        },
    }
    title, message, _, _, _ = format_event_for_bark(payload)
    assert title == "📁 File Uploaded"
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: History"
    assert lines[1] == "课件: syllabus_2026.pdf"
    assert lines[2].startswith("上传: ")


def test_format_event_announcement():
    payload = {
        "event": "announcement_created",
        "data": {
            "class_name": "Biology",
            "task_title": "Field Trip Permission Slips Due",
        },
    }
    title, message, _, _, _ = format_event_for_bark(payload)
    assert title == "📢 Class Announcement"
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "课程: Biology"
    assert lines[1] == "主题: Field Trip Permission Slips Due"
    assert lines[2].startswith("发布: ")


def test_format_event_test_ping():
    payload = {"event": "test_ping"}
    title, message, _, _, _ = format_event_for_bark(payload)
    assert title == "🔔 Test Notification"
    lines = message.split("\n")
    assert len(lines) == 3
    assert lines[0] == "通道: 实时推送正常"
    assert lines[1] == "设备: Mac & iPhone"
    assert lines[2].startswith("时间: ")


def test_cache_invalidation_on_mtime_change(tmp_path):
    import time

    alias_file = tmp_path / "aliases_mtime.json"
    alias_file.write_text(json.dumps({"Class A": "A"}), encoding="utf-8")

    # Initial load
    res1 = load_course_aliases(alias_file)
    assert res1 == {"Class A": "A"}

    # Update file with new mtime
    time.sleep(0.05)
    alias_file.write_text(json.dumps({"Class A": "Alpha", "Class B": "Beta"}), encoding="utf-8")
    # Touch or ensure mtime updated
    new_mtime = alias_file.stat().st_mtime + 1.0
    import os
    os.utime(alias_file, (new_mtime, new_mtime))

    res2 = load_course_aliases(alias_file)
    assert res2 == {"Class A": "Alpha", "Class B": "Beta"}


def test_webhook_handler_post(tmp_path):
    import io
    from bark_webhook_receiver import make_request_handler

    mock_pusher = MagicMock()
    mock_pusher.push.return_value = True

    alias_file = tmp_path / "aliases.json"
    alias_file.write_text(json.dumps({"English Language Arts Hons": "ELA Hons"}), encoding="utf-8")

    handler_cls = make_request_handler(
        mock_pusher, aliases_path=alias_file, secret="test-secret"
    )

    payload = {
        "event": "task_created",
        "event_id": "evt-webhook-handler-1",
        "data": {
            "class_name": "English Language Arts Hons",
            "task_title": "Novel Essay",
            "due_date": "2026-09-12T10:00:00",
        },
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    # The timestamp is signed material: it must be minted first and handed to
    # compute_signature together with the body.
    timestamp = f"{datetime.now().timestamp():.3f}"
    signature = compute_signature("test-secret", body_bytes, timestamp)

    # Simulate BaseHTTPRequestHandler
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {
        "Content-Length": str(len(body_bytes)),
        "X-MB-Event": "task_created",
        "X-MB-Signature": signature,
        "X-MB-Timestamp": timestamp,
    }
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    handler.do_POST()

    assert mock_pusher.push.called
    call_args = mock_pusher.push.call_args
    assert call_args[0][0] == "📝 New Task"
    msg = call_args[0][1]
    assert "课程: ELA Hons" in msg
    assert "作业: Novel Essay" in msg
    assert "教师" not in msg


def _post(handler_cls, body: bytes, headers: dict):
    """Drive do_POST with mocked socket plumbing; return the status sent."""
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {"Content-Length": str(len(body)), **headers}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    statuses: list[int] = []
    handler.send_response = lambda code, *a, **k: statuses.append(code)
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.do_POST()
    return statuses[0] if statuses else None


def test_webhook_rejects_unsigned_push():
    """A push with no signature must be refused — the daemon's signing is not decorative."""
    reset_replay_cache()
    pusher = MagicMock()
    handler_cls = make_request_handler(pusher, secret="s3cret")
    body = json.dumps({"event": "task_created", "event_id": "e1", "data": {}}).encode()
    status = _post(handler_cls, body, {"X-MB-Event": "task_created"})
    assert status == 401
    assert not pusher.push.called


def test_webhook_rejects_bad_signature():
    reset_replay_cache()
    pusher = MagicMock()
    handler_cls = make_request_handler(pusher, secret="s3cret")
    body = json.dumps({"event": "task_created", "event_id": "e2", "data": {}}).encode()
    status = _post(
        handler_cls,
        body,
        {
            "X-MB-Event": "task_created",
            "X-MB-Signature": "sha256=" + "0" * 64,
            "X-MB-Timestamp": f"{datetime.now().timestamp():.3f}",
        },
    )
    assert status == 401
    assert not pusher.push.called


def test_webhook_fails_closed_without_configured_secret():
    reset_replay_cache()
    pusher = MagicMock()
    handler_cls = make_request_handler(pusher, secret=None)
    body = json.dumps({"event": "task_created", "event_id": "e3", "data": {}}).encode()
    status = _post(handler_cls, body, {"X-MB-Event": "task_created"})
    assert status == 503
    assert not pusher.push.called


def test_webhook_rejects_replayed_event_id():
    reset_replay_cache()
    pusher = MagicMock()
    pusher.push.return_value = True
    handler_cls = make_request_handler(pusher, secret="s3cret")
    payload = {"event": "task_created", "event_id": "evt-replay-1", "data": {}}
    body = json.dumps(payload).encode()
    timestamp = f"{datetime.now().timestamp():.3f}"
    headers = {
        "X-MB-Event": "task_created",
        "X-MB-Signature": compute_signature("s3cret", body, timestamp),
        "X-MB-Timestamp": timestamp,
    }
    assert _post(handler_cls, body, dict(headers)) == 200
    # Same event_id again must be refused as a replay.
    assert _post(handler_cls, body, dict(headers)) == 409
    assert pusher.push.call_count == 1


def test_webhook_rejects_stale_timestamp():
    reset_replay_cache()
    pusher = MagicMock()
    handler_cls = make_request_handler(pusher, secret="s3cret")
    payload = {"event": "task_created", "event_id": "evt-stale-1", "data": {}}
    body = json.dumps(payload).encode()
    status = _post(
        handler_cls,
        body,
        {
            "X-MB-Event": "task_created",
            # Signed for a fresh moment, then restamped to the past: the digest
            # must fail first, so this reports a signature mismatch rather than
            # "merely old".
            "X-MB-Signature": compute_signature(
                "s3cret", body, f"{datetime.now().timestamp():.3f}"
            ),
            "X-MB-Timestamp": f"{datetime.now().timestamp() - 99999:.3f}",
        },
    )
    assert status == 401
    assert not pusher.push.called


# ── Signed material: X-MB-Timestamp is inside the HMAC ──────────────────
#
# The signature used to cover the body only, so X-MB-Timestamp was an
# unauthenticated sibling header. Anyone who captured one POST could rewrite
# that header to now and replay forever: the original digest still validated
# and the freshness check above waved it through. These tests pin the
# construction on the receiver side.


def test_verify_signature_accepts_a_correctly_signed_push():
    timestamp = f"{datetime.now().timestamp():.3f}"
    ok, reason = verify_signature(
        "s3cret",
        compute_signature("s3cret", b'{"a":1}', timestamp),
        b'{"a":1}',
        timestamp,
    )
    assert (ok, reason) == (True, "ok")


def test_verify_signature_rejects_a_restamped_payload():
    """The replay: swap the timestamp, keep the captured signature.

    The payload was captured long ago and is now replayed with
    ``X-MB-Timestamp`` rewritten to *now*, so it sits inside the freshness
    window. Under the old body-only construction the captured digest still
    validated and this replay was accepted.
    """
    body = json.dumps({"event": "task_created", "event_id": "evt-x", "data": {}}).encode()
    captured_at = "1700000000.000"
    captured = compute_signature("s3cret", body, captured_at)

    # The genuine timestamp at least clears the digest — proof that the
    # signature itself is valid and only the restamp breaks it.
    ok, reason = verify_signature("s3cret", captured, body, captured_at)
    assert ok is False
    assert reason == "stale_timestamp", "the digest should have matched"

    for offset in (0.0, -120.0, 120.0):
        forged = f"{datetime.now().timestamp() + offset:.3f}"
        ok, reason = verify_signature("s3cret", captured, body, forged)
        assert ok is False, "a fresh restamp of a captured payload was accepted"
        assert reason == "signature_mismatch", reason


def test_verify_signature_rejects_the_old_body_only_construction():
    """A receiver that still signs the body alone must not be accepted."""
    body = json.dumps({"event": "task_created", "event_id": "evt-x", "data": {}}).encode()
    timestamp = f"{datetime.now().timestamp():.3f}"
    body_only = "sha256=" + hmac.new(
        b"s3cret", body, hashlib.sha256
    ).hexdigest()
    ok, reason = verify_signature("s3cret", body_only, body, timestamp)
    assert ok is False
    assert reason == "signature_mismatch"


def test_verify_signature_fails_closed_without_a_timestamp():
    body = b'{"a":1}'
    signature = compute_signature("s3cret", body, "1700000000.000")
    # No timestamp header at all: nothing to verify against.
    ok, reason = verify_signature("s3cret", signature, body, None)
    assert ok is False
    assert reason == "missing_timestamp"


def test_signed_material_is_unambiguous():
    assert signed_material("17", b"89ab") != signed_material("1789", b"ab")
    assert signed_material("1700000000.000", b"{}") == b"1700000000.000.{}"
    # A missing timestamp must not silently produce a body-only signature.
    assert signed_material(None, b"{}") == b".{}"


def test_receiver_and_daemon_agree_on_the_signed_material():
    """End-to-end: the dispatcher signs, this receiver verifies.

    Skipped (not failed) when `tahuti` is not importable, so `extras/` tests
    stay runnable standalone.
    """
    pytest.importorskip("requests_mock", reason="needs requests-mock")
    try:
        from mb_cli.daemon.events import MBEvent, WebhookConfig
        from mb_cli.daemon.webhook import WebhookDispatcher
    except ImportError:
        pytest.skip("tahuti is not installed; cannot cross-check the producer")

    reset_replay_cache()
    pusher = MagicMock()
    pusher.push.return_value = True
    handler_cls = make_request_handler(pusher, secret="s3cret")

    event = MBEvent.create(
        "task_created",
        {"class_name": "English Language Arts Hons", "title": "Novel Essay"},
    )
    dispatcher = WebhookDispatcher(
        webhooks=[WebhookConfig(url="http://127.0.0.1:1/hook", secret="s3cret")]
    )
    with requests_mock.Mocker() as m:
        m.post("http://127.0.0.1:1/hook", status_code=200)
        dispatcher.dispatch(event)

    body = event.to_json().encode("utf-8")
    timestamp = m.last_request.headers["X-MB-Timestamp"]
    signature = m.last_request.headers["X-MB-Signature"]

    ok, reason = verify_signature("s3cret", signature, body, timestamp)
    assert (ok, reason) == (True, "ok"), reason

    # And the full handler path accepts the very same request.
    assert _post(
        handler_cls,
        body,
        {"X-MB-Event": "task_created", "X-MB-Signature": signature, "X-MB-Timestamp": timestamp},
    ) == 200
    assert pusher.push.called


def test_webhook_rejects_oversized_body():
    reset_replay_cache()
    pusher = MagicMock()
    handler_cls = make_request_handler(pusher, secret="s3cret")
    body = b"x" * 10
    status = _post(
        handler_cls,
        body,
        {
            "Content-Length": str(MAX_BODY_BYTES + 1),
            "X-MB-Event": "task_created",
            "X-MB-Signature": compute_signature("s3cret", body),
        },
    )
    assert status == 413
    assert not pusher.push.called


def test_sanitize_tap_url_blocks_offsite_and_non_https():
    assert sanitize_tap_url("https://demo-school.managebac.cn/x") == "https://demo-school.managebac.cn/x"
    assert sanitize_tap_url("https://evil.example.com/phish") == ""
    assert sanitize_tap_url("file:///etc/passwd") == ""
    assert sanitize_tap_url("http://demo-school.managebac.cn/x") == ""
    assert sanitize_tap_url("https://notmanagebac.com.evil.net/x") == ""
    assert sanitize_tap_url("") == ""


def test_log_safe_strips_control_chars():
    assert _log_safe("ok\nFAKE LOG LINE") == "okFAKE LOG LINE"
    assert "\x1b" not in _log_safe("a\x1b[31mb")


def test_format_event_fallback_when_task_title_matches_class_name():
    payload = {
        "event": "new_task",
        "data": {
            "class_name": "AP AP—Calculus BC (Grade 10) Yellow",
            "task_title": "AP AP—Calculus BC (Grade 10) Yellow",
            "body_preview": "Hongjing (Sarah) Shi has just added a new Task Unit 1.3 Library of functions in AP AP—Calculus BC (Grade 10) Yellow. When: September 13, 2026 at 11:55 PM View full details",
            "due_date": "September 13, 2026 at 11:55 PM",
        },
    }
    aliases = {"AP AP—Calculus BC (Grade 10) Yellow": "AP Calc BC"}
    title, message, _, _, _ = format_event_for_bark(payload, aliases=aliases)
    assert title == "📝 New Task"
    lines = message.split("\n")
    assert lines[0] == "课程: AP Calc BC"
    assert lines[1] == "作业: Unit 1.3 Library of functions"


def test_format_event_task_created_with_released_grade_shows_grade_posted():
    """If a score is released along with the task itself, show scores released banner instead of new task."""
    payload = {
        "event": "task_created",
        "data": {
            "class_name": "Chinese Language Arts I 高一语文1班 (Gr..",
            "task_title": "语文早读小测1",
            "due_date": "2026-09-11T10:10:00",
            "grade_letter": "A",
            "grade_score": "90 / 100 pts",
            "url": "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000013",
        },
    }
    title, message, sound, priority, url = format_event_for_bark(payload)

    assert title == "📊 Grade Posted"
    assert sound == "chime"
    assert priority == 7
    assert url == "https://demo-school.managebac.cn/student/classes/1000001/core_tasks/1000013"
    lines = message.split("\n")
    assert len(lines) == 3
    assert "课程:" in lines[0]
    assert lines[1] == "作业: 语文早读小测1"
    assert lines[2] == "得分: A 90 / 100 pts"


def test_format_event_task_created_with_enriched_grade_shows_grade_posted():
    """Ensure enriched_task grades also promote task_created to Grade Posted banner."""
    payload = {
        "event": "task_created",
        "data": {
            "class_name": "Chinese Language Arts I 高一语文1班 (Gr..",
            "task_title": "AP Chinese Language Arts I 高一语文1班 (Grade 10) E103",
            "body_preview": "Teacher has added a new Task 语文早读小测1 in Chinese Language Arts I",
            "due_date": "2026-09-11T10:10:00",
            "enriched_task": {
                "title": "语文早读小测1",
                "grade_letter": "A",
                "grade_score": "90 / 100 pts",
            },
        },
    }
    title, message, sound, priority, _ = format_event_for_bark(payload)

    assert title == "📊 Grade Posted"
    assert sound == "chime"
    assert priority == 7
    lines = message.split("\n")
    assert lines[1] == "作业: 语文早读小测1"
    assert lines[2] == "得分: A 90 / 100 pts"


def test_bark_receiver_suppression_logic():
    # 1. deadline_approaching with submitted status
    ev1 = {
        "event": "deadline_approaching",
        "data": {"status": "submitted", "task_id": 1},
    }
    assert is_task_event_suppressed(ev1) is True

    # 2. deadline_approaching with grade_score
    ev2 = {
        "event": "deadline_approaching",
        "data": {"grade_score": "7/7", "task_id": 2},
    }
    assert is_task_event_suppressed(ev2) is True

    # 3. deadline_approaching with enriched_task grade_letter
    ev3 = {
        "event": "deadline_approaching",
        "data": {"enriched_task": {"grade_letter": "A"}, "task_id": 3},
    }
    assert is_task_event_suppressed(ev3) is True

    # 4. deadline_approaching for unsubmitted/ungraded task -> NOT suppressed
    ev4 = {
        "event": "deadline_approaching",
        "data": {"status": "not-submitted", "grade_score": "-", "task_id": 4},
    }
    assert is_task_event_suppressed(ev4) is False

    # 5. task_updated for submitted task -> suppressed
    ev5 = {
        "event": "task_updated",
        "data": {"status": "submitted", "task_id": 5},
    }
    assert is_task_event_suppressed(ev5) is True

    # 6. task_updated for graded task -> suppressed
    ev6 = {
        "event": "task_updated",
        "data": {"grade_score": "95/100", "task_id": 6},
    }
    assert is_task_event_suppressed(ev6) is True

    # 7. task_graded / grade_posted -> NOT suppressed (must notify user!)
    ev7 = {
        "event": "task_graded",
        "data": {"grade_score": "95/100", "task_id": 7},
    }
    assert is_task_event_suppressed(ev7) is False



