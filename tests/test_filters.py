"""Tests for mb_cli.filters."""

from __future__ import annotations

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
        found = find_task_by_id(result, "27254393")
        assert found is sample_task

    def test_finds_in_past(self, make_crawl_result, sample_task):
        result = make_crawl_result(past=[sample_task])
        found = find_task_by_id(result, "27254393")
        assert found is sample_task

    def test_finds_in_overdue(self, make_crawl_result, sample_task):
        result = make_crawl_result(overdue=[sample_task])
        found = find_task_by_id(result, "27254393")
        assert found is sample_task

    def test_not_found(self, make_crawl_result, sample_task):
        result = make_crawl_result(upcoming=[sample_task])
        found = find_task_by_id(result, "99999999")
        assert found is None

    def test_empty_result(self, make_crawl_result):
        result = make_crawl_result()
        found = find_task_by_id(result, "123")
        assert found is None
