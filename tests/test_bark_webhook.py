import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bark_webhook_receiver import (
    DEFAULT_ALIASES_PATH,
    MAX_COURSE_LEN,
    MAX_TASK_LEN,
    clean_task_title,
    format_event_for_bark,
    load_course_aliases,
    resolve_course_name,
    truncate,
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

    handler_cls = make_request_handler(mock_pusher, aliases_path=alias_file)

    payload = {
        "event": "task_created",
        "data": {
            "class_name": "English Language Arts Hons",
            "task_title": "Novel Essay",
            "due_date": "2026-09-12T10:00:00",
        },
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    # Simulate BaseHTTPRequestHandler
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {"Content-Length": str(len(body_bytes)), "X-MB-Event": "task_created"}
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

