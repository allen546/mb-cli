"""Tests for unified task status domain model."""

from __future__ import annotations

from datetime import datetime, timedelta
import pytest

from mb_cli.task_status import (
    GradeStatus,
    SubmissionStatus,
    classify_task_view,
    get_grade_status,
    get_submission_status,
    get_task_display_grade,
    get_task_display_status,
    is_task_completed,
    is_task_submitted,
    is_task_todo,
)


class TestSubmissionStatus:
    def test_submitted_via_status(self):
        assert get_submission_status({"status": "submitted"}) == SubmissionStatus.SUBMITTED
        assert is_task_submitted({"status": "submitted"}) is True

    def test_submitted_via_label(self):
        assert get_submission_status({"labels": ["Formative", "Submitted"]}) == SubmissionStatus.SUBMITTED
        assert is_task_submitted({"labels": ["Formative", "Submitted"]}) is True

    def test_submitted_via_detail(self):
        task = {"detail": {"submission": {"id": 1}}}
        assert get_submission_status(task) == SubmissionStatus.SUBMITTED
        assert is_task_submitted(task) is True

    def test_pending_via_status(self):
        assert get_submission_status({"status": "not-submitted"}) == SubmissionStatus.PENDING
        assert is_task_submitted({"status": "not-submitted"}) is False

    def test_pending_via_submit_button(self):
        assert get_submission_status({"has_submit_button": True}) == SubmissionStatus.PENDING

    def test_none_for_offline_task(self):
        assert get_submission_status({"status": None, "has_submit_button": False}) == SubmissionStatus.NONE
        assert get_submission_status({}) == SubmissionStatus.NONE


class TestGradeStatus:
    def test_graded_letter_and_score(self):
        task = {"grade_letter": "A", "grade_score": "95 / 100 pts"}
        assert get_grade_status(task) == GradeStatus.GRADED

    def test_graded_score_only(self):
        task = {"grade_score": "100 / 100 pts"}
        assert get_grade_status(task) == GradeStatus.GRADED

    def test_graded_letter_only(self):
        task = {"grade_letter": "B+"}
        assert get_grade_status(task) == GradeStatus.GRADED

    def test_graded_complete_status(self):
        task = {"grade_letter": "Complete"}
        assert get_grade_status(task) == GradeStatus.GRADED

    def test_not_applicable(self):
        assert get_grade_status({"grade_letter": "N/A"}) == GradeStatus.NOT_APPLICABLE
        assert get_grade_status({"grade_letter": "not applicable"}) == GradeStatus.NOT_APPLICABLE

    def test_not_assessed_yet(self):
        assert get_grade_status({"grade_letter": "Not Assessed Yet"}) == GradeStatus.NOT_ASSESSED
        assert get_grade_status({"labels": ["Not Assessed Yet"]}) == GradeStatus.NOT_ASSESSED

    def test_zero_placeholder_score(self):
        assert get_grade_status({"grade_score": "0 / 100 pts"}) == GradeStatus.NOT_ASSESSED


class TestTaskLifecycleAndTodo:
    def test_unsubmitted_active_task_is_todo(self):
        task = {
            "title": "Kinematics quiz",
            "status": "not-submitted",
            "has_submit_button": True,
            "grade_letter": None,
            "grade_score": None,
        }
        assert is_task_todo(task) is True
        assert is_task_completed(task) is False

    def test_unsubmitted_even_if_marked_not_assessed_yet_is_todo(self):
        # Teacher has not assessed it yet, but student has not submitted: still todo!
        task = {
            "title": "HW 1",
            "status": "not-submitted",
            "grade_letter": "Not Assessed Yet",
        }
        assert is_task_todo(task) is True
        assert is_task_completed(task) is False

    def test_submitted_task_is_not_todo(self):
        task = {
            "title": "NAME LIST",
            "status": "submitted",
            "grade_letter": None,
            "grade_score": None,
        }
        assert is_task_todo(task) is False
        assert is_task_completed(task) is True

    def test_graded_task_is_not_todo(self):
        task = {
            "title": "Exam 1",
            "status": "not-submitted",
            "grade_letter": "A",
            "grade_score": "100 / 100 pts",
        }
        assert is_task_todo(task) is False
        assert is_task_completed(task) is True

    def test_offline_task_marked_na_is_not_todo(self):
        task = {
            "title": "Vocab Quiz",
            "grade_letter": "N/A",
            "status": None,
            "has_submit_button": False,
        }
        assert is_task_todo(task) is False
        assert is_task_completed(task) is True


class TestClassifyTaskView:
    def test_upcoming_due_in_future(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        task = {
            "due_date": "2026-09-15 12:00:00",
            "status": "not-submitted",
        }
        assert classify_task_view(task, now_ref=now) == "upcoming"

    def test_overdue_due_in_past_and_todo(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        task = {
            "due_date": "2026-09-05 12:00:00",
            "status": "not-submitted",
        }
        assert classify_task_view(task, now_ref=now) == "overdue"

    def test_past_due_in_past_and_completed(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        task = {
            "due_date": "2026-09-05 12:00:00",
            "status": "submitted",
        }
        assert classify_task_view(task, now_ref=now) == "past"

    def test_past_due_in_past_with_grade(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        task = {
            "due_date": "2026-09-05 12:00:00",
            "grade_letter": "A",
            "grade_score": "100 / 100 pts",
        }
        assert classify_task_view(task, now_ref=now) == "past"


class TestDisplayFormatting:
    def test_graded_display(self):
        t1 = {"grade_letter": "A", "grade_score": "100 / 100 pts"}
        assert get_task_display_grade(t1) == "A ( 100 / 100 pts )"
        assert get_task_display_status(t1) == "Complete (Graded)"

        t2 = {"grade_letter": "B"}
        assert get_task_display_grade(t2) == "B"

        t3 = {"grade_score": "90 / 100 pts"}
        assert get_task_display_grade(t3) == "90 / 100 pts"

    def test_na_display(self):
        t = {"grade_letter": "N/A"}
        assert get_task_display_grade(t) == "N/A"
        assert get_task_display_status(t) == "Complete"

    def test_ungraded_submitted_display(self):
        t = {"status": "submitted"}
        assert get_task_display_grade(t) == "Ungraded"
        assert get_task_display_status(t) == "Complete (Submitted)"

    def test_unsubmitted_upcoming_display(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        t = {
            "status": "not-submitted",
            "due_date": "2026-09-15 12:00:00",
        }
        assert get_task_display_grade(t, now_ref=now) == "Unsubmitted"
        assert get_task_display_status(t) == "Incomplete (Todo)"

    def test_unsubmitted_overdue_display(self):
        now = datetime(2026, 9, 10, 12, 0, 0)
        t = {
            "status": "not-submitted",
            "due_date": "2026-09-05 12:00:00",
        }
        assert get_task_display_grade(t, now_ref=now) == "⚠ Unsubmitted"
        assert get_task_display_status(t) == "Incomplete (Todo)"


class TestMathematicalEquivalenceInvariant:
    @pytest.mark.parametrize(
        "task,now_ref,expected_todo",
        [
            ({"status": "not-submitted", "due_date": "2026-09-15 12:00:00"}, datetime(2026, 9, 10), True),
            ({"status": "not-submitted", "due_date": "2026-09-05 12:00:00"}, datetime(2026, 9, 10), True),
            ({"status": "submitted", "due_date": "2026-09-05 12:00:00"}, datetime(2026, 9, 10), False),
            ({"grade_letter": "A", "grade_score": "100 / 100 pts"}, datetime(2026, 9, 10), False),
            ({"grade_letter": "N/A"}, datetime(2026, 9, 10), False),
            ({"status": None, "has_submit_button": False}, datetime(2026, 9, 10), False),
        ],
    )
    def test_unsubmitted_display_matches_is_todo(self, task, now_ref, expected_todo):
        display_grade = get_task_display_grade(task, now_ref=now_ref)
        is_todo = is_task_todo(task)
        assert is_todo == expected_todo
        if expected_todo:
            assert display_grade in ("Unsubmitted", "⚠ Unsubmitted")
            assert get_task_display_status(task) == "Incomplete (Todo)"
        else:
            assert display_grade not in ("Unsubmitted", "⚠ Unsubmitted")
            assert "Complete" in get_task_display_status(task)
