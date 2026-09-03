"""Tests for mb_cli.filters."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure worktree src is prioritized over editable installs in venv
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from mb_cli.filters import (
    filter_result_by_subject,
    find_task_by_id,
    matches_subject,
    result_views,
)


class TestMatchesSubject:
    def test_exact_match(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "Math HL") is True

    def test_case_insensitive(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "math hl") is True

    def test_partial_match(self):
        task = {"class_name": "CAIE IGCSE G9 EL-L0"}
        assert matches_subject(task, "EL") is True

    def test_no_match(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "Physics") is False

    def test_missing_class_name(self):
        task = {}
        assert matches_subject(task, "Math") is False

    def test_none_class_name(self):
        task = {"class_name": None}
        assert matches_subject(task, "Math") is False

    def test_empty_subject(self):
        task = {"class_name": "Math HL"}
        assert matches_subject(task, "") is True


class TestFilterResultBySubject:
    def test_filters_all_views(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[
                {"class_name": "Math HL", "title": "T1"},
                {"class_name": "English A", "title": "T2"},
            ],
            past=[
                {"class_name": "Math HL", "title": "T3"},
                {"class_name": "Physics", "title": "T4"},
            ],
            overdue=[
                {"class_name": "Math HL", "title": "T5"},
            ],
        )
        filtered = filter_result_by_subject(result, "Math")
        assert len(filtered["upcoming"]) == 1
        assert len(filtered["past"]) == 1
        assert len(filtered["overdue"]) == 1
        assert filtered["summary"]["total_count"] == 3
        assert filtered["subject_filter"] == "Math"

    def test_no_matches(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"class_name": "English", "title": "T1"}],
            past=[],
            overdue=[],
        )
        filtered = filter_result_by_subject(result, "ZZZZZ")
        assert len(filtered["upcoming"]) == 0
        assert filtered["summary"]["total_count"] == 0


class TestResultViews:
    def test_all_view(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "all")
        assert len(views["upcoming"]) == 1
        assert len(views["past"]) == 1
        assert len(views["overdue"]) == 1

    def test_upcoming_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "upcoming")
        assert len(views["upcoming"]) == 1
        assert len(views["past"]) == 0
        assert len(views["overdue"]) == 0

    def test_past_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "past")
        assert len(views["upcoming"]) == 0
        assert len(views["past"]) == 1
        assert len(views["overdue"]) == 0

    def test_overdue_only(self, make_crawl_result):
        result = make_crawl_result(
            upcoming=[{"t": 1}],
            past=[{"t": 2}],
            overdue=[{"t": 3}],
        )
        views = result_views(result, "overdue")
        assert len(views["upcoming"]) == 0
        assert len(views["past"]) == 0
        assert len(views["overdue"]) == 1


class TestFindTaskById:
    def test_finds_in_upcoming(self, make_crawl_result, sample_task):
        result = make_crawl_result(upcoming=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_finds_in_past(self, make_crawl_result, sample_task):
        result = make_crawl_result(past=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_finds_in_overdue(self, make_crawl_result, sample_task):
        result = make_crawl_result(overdue=[sample_task])
        found = find_task_by_id(result, "1000026")
        assert found is sample_task

    def test_not_found(self, make_crawl_result, sample_task):
        result = make_crawl_result(upcoming=[sample_task])
        found = find_task_by_id(result, "99999999")
        assert found is None

    def test_empty_result(self, make_crawl_result):
        result = make_crawl_result()
        found = find_task_by_id(result, "123")
        assert found is None


class TestMatchesTag:
    def test_matches_tag_exact(self):
        from mb_cli.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Exam") is True

    def test_matches_tag_case_insensitive(self):
        from mb_cli.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "exam") is True

    def test_matches_tag_partial(self):
        from mb_cli.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Sum") is True

    def test_matches_tag_no_match(self):
        from mb_cli.filters import matches_tag
        task = {"labels": ["Summative", "Exam"]}
        assert matches_tag(task, "Quiz") is False

    def test_matches_tag_no_labels(self):
        from mb_cli.filters import matches_tag
        task = {}
        assert matches_tag(task, "Exam") is False


def test_is_submitted_badge():
    from mb_cli.filters import is_submitted_badge
    assert is_submitted_badge("Submitted") is True
    assert is_submitted_badge("submitted") is True
    assert is_submitted_badge("Not Submitted") is False
    assert is_submitted_badge("unsubmitted") is False
    assert is_submitted_badge("Pending") is False


def test_is_task_submitted():
    from mb_cli.filters import is_task_submitted
    assert is_task_submitted({"status": "submitted"}) is True
    assert is_task_submitted({"labels": ["Submitted"]}) is True
    assert is_task_submitted({"labels": ["Not Submitted"]}) is False
    assert is_task_submitted({"detail": {"submission": {"id": 123}}}) is True
    assert is_task_submitted({}) is False


def test_is_task_unfinished_and_completed():
    from mb_cli.filters import is_task_completed, is_task_unfinished

    # Incomplete task: has submit button, not submitted, no passing grade, assessed
    task_todo = {
        "has_submit_button": True,
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "labels": [],
    }
    assert is_task_unfinished(task_todo) is True
    assert is_task_completed(task_todo) is False

    # Completed: submitted
    task_submitted = dict(task_todo, status="submitted")
    assert is_task_unfinished(task_submitted) is False
    assert is_task_completed(task_submitted) is True

    # Completed: has valid grade letter
    task_graded = dict(task_todo, grade_letter="A")
    assert is_task_unfinished(task_graded) is False
    assert is_task_completed(task_graded) is True

    # Completed: not assessed yet (exempt)
    task_exempt = dict(task_todo, labels=["Not Assessed Yet"])
    assert is_task_unfinished(task_exempt) is False
    assert is_task_completed(task_exempt) is True


def test_classify_task_view():
    from datetime import datetime
    from mb_cli.filters import classify_task_view

    now = datetime(2026, 9, 10, 12, 0, 0)

    # Future task -> upcoming
    t_future = {
        "due_date": "2026-09-15 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_future, now_ref=now) == "upcoming"

    # Past deadline, unfinished -> overdue
    t_overdue = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_overdue, now_ref=now) == "overdue"

    # Past deadline, completed -> past
    t_past = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "submitted",
    }
    assert classify_task_view(t_past, now_ref=now) == "past"


def test_matches_submitted_not_submitted_bugfix():
    from mb_cli.filters import matches_completed, matches_submitted
    task_not_sub = {"labels": ["Not Submitted"]}
    assert matches_submitted(task_not_sub, True) is False
    assert matches_submitted(task_not_sub, False) is True

    task_sub = {"labels": ["Submitted"]}
    assert matches_submitted(task_sub, True) is True
    assert matches_submitted(task_sub, False) is False

    task_todo = {
        "has_submit_button": True,
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "labels": [],
    }
    assert matches_completed(task_todo, False) is True
    assert matches_completed(task_todo, True) is False

