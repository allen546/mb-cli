"""Submission-state parsing against the live ManageBac markup.

Every state-bearing fragment in this file is verbatim markup captured from the
``myschool.managebac.cn`` class-grades and tasks-list pages on 2026-09-19.
Nothing here is invented markup; the surrounding card/tile scaffolding is only
what the parser needs to find a title.

The two facts that made the old parser wrong, both of which the fixtures below
pin:

* ManageBac never renders a CSS class containing "submitted" for the submitted
  case.  On a class-grades page the submitted state is a green box badge whose
  nested ``badge-label`` reads "Submitted" — there is no ``span.submitted``
  anywhere on any class-grades page, so ``find(class_=...submitted...)`` returns
  ``None`` for all 11 genuinely-submitted tasks.
* The unsubmitted state is ``<span class="cell not-submitted">`` whose *text* is
  "Not Submitted" — capital N, capital S, space and not hyphen.  Storing that
  text verbatim made the ``status == "not-submitted"`` test downstream
  permanently dead, and for a card whose teacher had closed the dropbox link
  there was nothing left to rescue it: a task the page labels ``Not Submitted``
  displayed as plain "Complete".

Six real tasks carry no badge, a "Not Assessed Yet" cell and no dropbox link at
all.  Whether those should display "Complete", "Incomplete (Todo)" or an honest
"Unknown" is an open design question for the project owner, so these tests pin
only what the page says and leave the display string exactly as it was.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sys

# Ensure worktree src is prioritized over editable installs in venv
worktree_src = str(Path(__file__).resolve().parent.parent / "src")
if sys.path[0] != worktree_src:
    sys.path.insert(0, worktree_src)

import importlib

import mb_cli

mb_cli_pkg_dir = str(Path(worktree_src) / "mb_cli")
if hasattr(mb_cli, "__path__") and mb_cli_pkg_dir not in mb_cli.__path__:
    mb_cli.__path__.insert(0, mb_cli_pkg_dir)

if "mb_cli.client" in sys.modules:
    importlib.reload(sys.modules["mb_cli.client"])

from unittest.mock import patch

import pytest
import requests_mock as rm
from bs4 import BeautifulSoup

from mb_cli.cache import ResponseCache
from mb_cli.client import ManageBacClient
from mb_cli.task_status import (
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_SUBMITTED,
    SubmissionStatus,
    format_grade_display,
    get_submission_status,
    get_task_display_status,
    is_task_todo,
    normalize_submission_status,
    submission_status_from_labels,
)

CLASS_ID = "1000001"
GRADES_URL = f"https://myschool.managebac.cn/student/classes/{CLASS_ID}/core_tasks"


# ── Verbatim markup captured from the live pages ─────────────────────────

# Class-grades page, submitted task.  No span on this fragment — or anywhere on
# any class-grades page — carries a class containing "submitted".
SUBMITTED_BADGE = (
    '<span class="badge color-box-green" data-bs-title="18 hours early" '
    'data-controller="tooltip"><svg aria-hidden="true" class="fi fi-check_circle_fill" '
    'viewbox="0 0 16 16" width="16" xmlns="http://www.w3.org/2000/svg"></svg> '
    '<span class="badge-label">Submitted</span></span>'
)

# Class-grades page, unsubmitted task: the cell (whose class says it) and the
# grey pending badge (whose class says nothing).
NOT_SUBMITTED_CELL = '<span class="cell not-submitted">Not Submitted</span>'
PENDING_BADGE = (
    '<span class="badge color-box-gray" data-bs-title="Waiting" data-controller="tooltip">'
    '<svg aria-hidden="true" class="fi fi-clock_fill" viewbox="0 0 16 16" width="16" '
    'xmlns="http://www.w3.org/2000/svg"></svg> <span class="badge-label">Pending</span></span>'
)

# Class-grades page, graded task.
GRADED_CELL = (
    '<div class="cell labels-set"><span class="grade grade-success">A</span>'
    '<div class="points">95 / 100 pts</div></div>'
)

# Class-grades page, not assessed.  These six tasks have no submission badge and
# no dropbox link anywhere — their submission state is genuinely unknown.
NOT_ASSESSED_CELL = (
    '<div class="assessment task-score score-mobile-compact assessment-cell">'
    "Not Assessed Yet</div>"
)

# Tasks-list page, the four tile-suffix variants.
TILE_SUFFIX_ASSESSMENT = (
    '<div class="f-tile__suffix f-tile__suffix--extended"><div class="d-flex flex-grow-1">'
    '<div class="f-task-score f-task-score--assessment"><h4 class="color-success">D</h4>'
    '<p class="fw-semibold">24<span class="color-secondary">/35</span> pts</p></div></div></div>'
)
TILE_SUFFIX_NOT_ASSESSED = (
    '<div class="f-tile__suffix f-tile__suffix--extended"><div class="d-flex flex-grow-1">'
    '<div class="f-task-score f-task-score--not-assessed">'
    '<div class="f-task-score__body color-secondary"><svg></svg>'
    '<p class="fw-semibold">Not Assessed Yet</p></div></div></div></div>'
)
TILE_SUFFIX_SUBMITTED = (
    '<div class="f-tile__suffix f-tile__suffix--extended"><div class="d-flex flex-grow-1">'
    '<div class="f-task-score f-task-score--submitted">'
    '<div class="f-task-score__body color-success"><svg></svg>'
    '<p class="fw-semibold">Submitted</p></div></div></div></div>'
)
# The pending variant: no grade element and no text at all.
TILE_SUFFIX_DUE = (
    '<div class="f-tile__suffix"><div class="f-task-score f-task-score--due"></div></div>'
)

TILE_DESCRIPTION = (
    '<div class="f-tile__description"><span>Sep 07</span>'
    '<a href="/student/classes/1000012/">Chinese A</a></div>'
)


def _card(task_id: str, title: str, *fragments: str, dropbox: bool = False) -> str:
    """A class-grades card carrying *fragments* of live markup.

    ``dropbox`` adds the live dropbox link the 11 unsubmitted tasks really have;
    the anchor is deliberately textless so no assertion here depends on link
    wording that was not captured.
    """
    link = f'<a href="/student/classes/{CLASS_ID}/core_tasks/{task_id}/dropbox"></a>'
    return (
        '<div class="fusion-card-item">'
        f'<h4 class="title"><a href="/student/classes/{CLASS_ID}/core_tasks/{task_id}">{title}</a></h4>'
        + "".join(fragments)
        + (link if dropbox else "")
        + "</div>"
    )


def _tile(task_id: str, title: str, suffix: str) -> str:
    return (
        '<div class="f-task-tile">'
        f'<a class="f-tile__title-link" href="/student/classes/1000012/core_tasks/{task_id}">{title}</a>'
        + TILE_DESCRIPTION
        + suffix
        + "</div>"
    )


@pytest.fixture()
def client(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    c = ManageBacClient("myschool", domain="managebac.cn", cache=cache, verify=False, retry=0)
    c.set_cookie("test_session_cookie")
    return c


def _parse_cards(client, cards: list[str]) -> list[dict]:
    """Run the real ``get_class_grades`` over *cards* and return parsed tasks."""
    with rm.Mocker() as m:
        m.get(GRADES_URL, text="<html><body>" + "".join(cards) + "</body></html>")
        return client.get_class_grades(CLASS_ID, bypass_cache=True)["tasks"]


def _parse_tiles(client, tiles: list[str]) -> list[dict]:
    soup = BeautifulSoup("<html><body>" + "".join(tiles) + "</body></html>", "html.parser")
    return [t for tile in soup.find_all("div", class_=re.compile(r"f-task-tile"))
            if (t := client._parse_tile(tile))]


# ── Consequence 3: the submitted state is never read at all ──────────────


class TestSubmittedBadgeIsTheSignal:
    """The submitted state lives in a badge whose class never says "submitted"."""

    def test_no_element_on_a_submitted_card_carries_a_submitted_class(self):
        soup = BeautifulSoup(_card("1000101", "Reading response", SUBMITTED_BADGE), "html.parser")
        assert soup.find(class_=re.compile(r"\bsubmitted\b")) is None, (
            "the live page has no span whose class contains 'submitted' — if this "
            "fixture ever grows one, the fixture stopped being live markup"
        )

    def test_submitted_badge_detected_without_any_submitted_class(self, client):
        tasks = _parse_cards(client, [_card("1000101", "Reading response", SUBMITTED_BADGE)])
        task = tasks[0]

        # No labels to fall back on either: the badge is the only signal.
        assert task["labels"] is None
        assert task["status"] == SUBMISSION_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.SUBMITTED
        assert get_task_display_status(task) == "Complete (Submitted)"

    def test_submitted_badge_inside_labels_set_is_detected_too(self, client):
        """The live card also carries the badge inside its ``labels-set`` cell.

        Pre-fix that label text was the *only* reason the 11 submitted tasks were
        detected at all — incidentally, through a label, never through the badge.
        Both shapes must now read the same.
        """
        cell = f'<div class="cell labels-set">{SUBMITTED_BADGE}</div>'
        tasks = _parse_cards(client, [_card("1000108", "Reading response", cell)])
        task = tasks[0]
        assert "Submitted" in (task["labels"] or [])
        assert task["status"] == SUBMISSION_SUBMITTED
        assert get_task_display_status(task) == "Complete (Submitted)"

    def test_pending_badge_alone_also_names_the_unsubmitted_state(self, client):
        """The grey badge carries no state class either — only its label says it."""
        tasks = _parse_cards(client, [_card("1000102", "Essay plan", PENDING_BADGE)])
        assert tasks[0]["status"] == SUBMISSION_NOT_SUBMITTED
        assert get_task_display_status(tasks[0]) == "Incomplete (Todo)"

    def test_cell_text_is_canonicalised_not_stored_verbatim(self, client):
        """The cell's text is "Not Submitted"; the stored token is "not-submitted"."""
        tasks = _parse_cards(
            client, [_card("1000103", "Vocabulary quiz", NOT_SUBMITTED_CELL, PENDING_BADGE)]
        )
        task = tasks[0]
        assert task["status"] == SUBMISSION_NOT_SUBMITTED
        assert task["status"] != "Not Submitted"
        assert get_submission_status(task) == SubmissionStatus.PENDING
        assert is_task_todo(task) is True
        assert get_task_display_status(task) == "Incomplete (Todo)"

    def test_badge_label_text_is_not_mistaken_for_a_grade(self, client):
        """A submitted card has no grade; "Submitted" must not become one."""
        tasks = _parse_cards(client, [_card("1000104", "Poster", SUBMITTED_BADGE)])
        assert tasks[0]["grade_letter"] is None
        assert tasks[0]["points"] is None

    def test_graded_card_is_graded_not_submitted(self, client):
        tasks = _parse_cards(client, [_card("1000105", "Unit test", GRADED_CELL)])
        task = tasks[0]
        assert task["grade_letter"] == "A"
        assert task["points"] == "95 / 100 pts"
        assert task["status"] is None
        assert get_task_display_status(task) == "Complete (Graded)"


# ── Consequence 2: the latent wrong answer, already reachable ────────────


class TestClosedDropboxStillClassifiesPending:
    """Six real tasks have no dropbox link because their teacher closed it.

    With ``has_submit_button`` False and the raw page text "Not Submitted", a
    task the page explicitly labels ``Not Submitted`` used to classify as
    ``none`` — which ``get_task_display_status`` prints as plain "Complete" and
    ``format_grade_display`` as "Ungraded".
    """

    def _closed_dropbox_task(self, client) -> dict:
        tasks = _parse_cards(
            client,
            [_card("1000106", "Chinese oral", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=False)],
        )
        task = tasks[0]
        assert task["has_submit_button"] is False, "this case has no dropbox link"
        return task

    def test_closed_dropbox_task_is_pending(self, client):
        task = self._closed_dropbox_task(client)
        assert task["status"] == SUBMISSION_NOT_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.PENDING
        assert is_task_todo(task) is True

    def test_closed_dropbox_task_is_never_complete(self, client):
        task = self._closed_dropbox_task(client)
        display = get_task_display_status(task)
        assert display == "Incomplete (Todo)"
        assert display not in ("Complete", "Complete (Graded)", "Complete (Submitted)")

    def test_closed_dropbox_task_grade_is_unsubmitted_not_ungraded(self, client):
        task = self._closed_dropbox_task(client)
        grade = format_grade_display(task)
        assert grade != "Ungraded"
        assert grade in ("Unsubmitted", "⚠ Unsubmitted")

    def test_get_class_tasks_keeps_the_state_for_raw_page_text(self, client):
        """The ``get_class_tasks`` reconstruction compares tokens, not page text.

        Fed a task still carrying the verbatim cell text "Not Submitted" and no
        dropbox link — exactly what the old parse produced — the reconstruction
        must still record "not-submitted" instead of dropping the state.
        """
        raw = {
            "task_id": "1000106",
            "title": "Chinese oral",
            "url": f"{client.base}/student/classes/{CLASS_ID}/core_tasks/1000106",
            "due_date": "Sep 07",
            "grade_letter": None,
            "points": None,
            "status": "Not Submitted",
            "category": None,
            "labels": None,
            "has_submit_button": False,
        }
        with patch.object(client, "get_class_grades", return_value={"tasks": [raw]}):
            tasks = client.get_class_tasks(CLASS_ID, class_name="Chinese A", bypass_cache=True)

        assert tasks[0]["status"] == SUBMISSION_NOT_SUBMITTED
        assert get_task_display_status(tasks[0]) == "Incomplete (Todo)"


# ── Out of scope: genuinely unknown submission state ─────────────────────


class TestUnlabelledTaskStaysUnknown:
    """No badge, "Not Assessed Yet" cell, no dropbox link: the page says nothing.

    Whether that should display "Complete", "Incomplete (Todo)" or an honest
    "Unknown" is the project owner's call, so the display string is pinned to
    what it is today and the parser reports no state rather than guessing one.
    """

    def test_parser_invents_no_state(self, client):
        tasks = _parse_cards(client, [_card("1000107", "Oral presentation", NOT_ASSESSED_CELL)])
        task = tasks[0]
        assert task["has_submit_button"] is False
        assert task["status"] is None
        assert get_submission_status(task) == SubmissionStatus.NONE

    def test_display_string_is_unchanged(self, client):
        tasks = _parse_cards(client, [_card("1000107", "Oral presentation", NOT_ASSESSED_CELL)])
        assert get_task_display_status(tasks[0]) == "Complete"

    def test_not_assessed_yet_is_not_a_submission_state(self):
        assert normalize_submission_status("Not Assessed Yet") is None


# ── The acceptance criterion: the live-verified classification table ─────


class TestLiveClassificationTable:
    """45 real tasks: 17 graded, 11 submitted, 11 unsubmitted, 6 unlabelled.

    Complete (Graded) ×17, Complete (Submitted) ×11, Incomplete (Todo) ×11,
    Complete ×6 — every row in the same place, plus the latent closed-dropbox
    case now correct.
    """

    CARDS: list[str] = []
    EXPECTED: dict[str, str] = {}
    for _i in range(17):
        CARDS.append(_card(f"{2100 + _i}", f"Graded {_i}", GRADED_CELL))
        EXPECTED[f"Graded {_i}"] = "Complete (Graded)"
    for _i in range(11):
        CARDS.append(_card(f"{3100 + _i}", f"Submitted {_i}", SUBMITTED_BADGE))
        EXPECTED[f"Submitted {_i}"] = "Complete (Submitted)"
    for _i in range(11):
        CARDS.append(
            _card(f"{4100 + _i}", f"Todo {_i}", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=True)
        )
        EXPECTED[f"Todo {_i}"] = "Incomplete (Todo)"
    for _i in range(6):
        CARDS.append(_card(f"{5100 + _i}", f"Unlabelled {_i}", NOT_ASSESSED_CELL))
        EXPECTED[f"Unlabelled {_i}"] = "Complete"
    # The latent case: page says Not Submitted, teacher closed the dropbox.
    CARDS.append(_card("6100", "Closed dropbox", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=False))
    EXPECTED["Closed dropbox"] = "Incomplete (Todo)"

    def test_the_45_verified_rows_stay_where_they_are(self, client):
        tasks = _parse_cards(client, self.CARDS)
        assert len(tasks) == 46  # the 45 real tasks plus the latent closed-dropbox case

        counts = Counter(get_task_display_status(t) for t in tasks)
        assert counts["Complete (Graded)"] == 17
        assert counts["Complete (Submitted)"] == 11
        assert counts["Incomplete (Todo)"] == 12  # the 11 real ones + the latent case
        assert counts["Complete"] == 6

    def test_every_row_lands_in_its_verified_bucket(self, client):
        tasks = _parse_cards(client, self.CARDS)
        actual = {t["title"]: get_task_display_status(t) for t in tasks}
        assert actual == self.EXPECTED

    def test_parsed_status_tokens_are_canonical(self, client):
        tasks = {t["title"]: t for t in _parse_cards(client, self.CARDS)}

        assert {t["status"] for name, t in tasks.items() if name.startswith("Submitted")} == {
            SUBMISSION_SUBMITTED
        }
        assert {
            t["status"] for name, t in tasks.items() if name.startswith(("Todo", "Closed"))
        } == {SUBMISSION_NOT_SUBMITTED}
        # Graded and unlabelled cards say nothing about submission; that stays
        # None rather than being coerced into "not-submitted".
        assert {t["status"] for name, t in tasks.items() if name.startswith(("Graded", "Unlabelled"))} == {
            None
        }

    def test_all_11_unsubmitted_rows_are_genuinely_unsubmitted(self, client):
        """Every Incomplete (Todo) row carries the page's own Not Submitted cell."""
        tasks = _parse_cards(client, self.CARDS)
        todo = [t for t in tasks if get_task_display_status(t) == "Incomplete (Todo)"]
        assert len(todo) == 12
        assert all(t["status"] == SUBMISSION_NOT_SUBMITTED for t in todo)
        assert all(get_submission_status(t) == SubmissionStatus.PENDING for t in todo)

    def test_all_11_submitted_rows_are_detected(self, client):
        tasks = _parse_cards(client, self.CARDS)
        submitted = [t for t in tasks if get_task_display_status(t) == "Complete (Submitted)"]
        assert len(submitted) == 11
        assert all(get_submission_status(t) == SubmissionStatus.SUBMITTED for t in submitted)


# ── Consequence 4: the tile path and its four suffix variants ────────────


class TestTileSuffixVariants:
    """The tile suffix variant class is the signal; the submit-button heuristic
    is dead — none of the 47 tiles on a live tasks page carries a dropbox link.
    """

    def test_submitted_variant(self, client):
        (tile,) = _parse_tiles(client, [_tile("1000099", "Reading log", TILE_SUFFIX_SUBMITTED)])
        assert tile["status"] == SUBMISSION_SUBMITTED
        assert tile["submission_status"] == "submitted"
        # The badge word must not leak into the grade fields.
        assert tile["grade_letter"] is None
        assert tile["grade_score"] is None
        assert tile["has_submit_button"] is False

    def test_assessment_variant_parses_the_grade_and_claims_no_submission_state(self, client):
        (tile,) = _parse_tiles(client, [_tile("1000098", "Unit test", TILE_SUFFIX_ASSESSMENT)])
        assert tile["grade_letter"] == "D"
        assert tile["grade_score"] == "24 /35 pts"
        # Graded, so nothing to submit and no submission state to report.
        assert tile["status"] is None
        assert tile["submission_status"] == "none"
        assert tile["has_submit_button"] is False

    def test_not_assessed_variant_keeps_the_page_text_out_of_the_grade(self, client):
        (tile,) = _parse_tiles(
            client, [_tile("1000097", "Oral presentation", TILE_SUFFIX_NOT_ASSESSED)]
        )
        # "Not Assessed Yet" is parsed the way the class-grades page parses it,
        # and is never treated as a score.
        assert tile["grade_letter"] == "Not Assessed Yet"
        assert tile["grade_score"] is None
        assert tile["status"] is None
        assert tile["submission_status"] == "none"

    def test_due_variant_is_the_pending_state(self, client):
        """No grade element and no text at all — the variant class is all there is."""
        (tile,) = _parse_tiles(client, [_tile("1000096", "Essay draft", TILE_SUFFIX_DUE)])
        assert tile["grade_letter"] is None
        assert tile["grade_score"] is None
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["submission_status"] == "pending"
        # A pending tile is offered an upload, as it always was.
        assert tile["has_submit_button"] is True

    def test_every_variant_is_read_from_the_class_alone(self, client):
        """Strip the variant modifier and the state disappears with it.

        Proves the detection is reading the ``f-task-score--<variant>`` class
        rather than some leftover text heuristic.
        """
        variants = {
            TILE_SUFFIX_SUBMITTED: SUBMISSION_SUBMITTED,
            TILE_SUFFIX_DUE: SUBMISSION_NOT_SUBMITTED,
        }
        for suffix, expected in variants.items():
            stripped = re.sub(r"f-task-score--[a-z-]+", "f-task-score", suffix)
            (tile,) = _parse_tiles(client, [_tile("1000095", "Essay draft", stripped)])
            assert tile["status"] != expected

    def test_legacy_suffix_markup_still_works(self, client):
        """Older tiles with no score box keep their badge/link driven status."""
        legacy_submitted = _tile(
            "1000094",
            "Homework of summer holiday",
            '<div class="f-tile__suffix"><span class="badge">Submitted</span></div>',
        )
        (tile,) = _parse_tiles(client, [legacy_submitted])
        assert tile["status"] == SUBMISSION_SUBMITTED
        assert tile["submission_status"] == "submitted"

        legacy_pending = _tile(
            "1000093",
            "Poster",
            '<div class="f-tile__suffix"><a class="btn btn-primary" '
            'href="/student/classes/1000012/core_tasks/1000093/dropbox">Submit Coursework</a></div>',
        )
        (tile,) = _parse_tiles(client, [legacy_pending])
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["submission_status"] == "pending"
        assert tile["has_submit_button"] is True

    def test_a_task_list_tile_is_no_longer_reported_submitted_by_its_action_label(self, client):
        """"Submit Coursework" is an action, not a state."""
        assert submission_status_from_labels(["Formative", "Submit Coursework"]) is None
        assert submission_status_from_labels(["Formative", "Upload submission"]) is None


class TestTileTableMovesAsTheOwnerExpects:
    """The tile path is *expected* to change: it called nearly everything todo.

    Under the live UI the tile path asserted "not-submitted" for anything it
    could not prove submitted, so all 45 of these tiles carried that token — and
    since the classifier re-derives state from it, all 45 read as pending — while
    ``has_submit_button`` stayed False for every one of them.  Measured on this
    fixture: 28 of the 45 displayed as "Incomplete (Todo)" (including all 11
    genuinely-submitted ones) and 28 landed in ``overdue``; after the fix the
    same 45 read 17 graded / 11 submitted / 11 due / 6 unlabelled, with 11 in
    ``overdue``.

    Only the class-grades table is the no-regression bar; this pins the corrected
    tile reading so the movement is deliberate and visible.
    """

    def _tiles_for(self, client):
        """The same 45 tasks re-rendered as tasks-list tiles."""
        tiles = []
        for i in range(17):
            tiles.append(_tile(f"{2100 + i}", f"Graded {i}", TILE_SUFFIX_ASSESSMENT))
        for i in range(11):
            tiles.append(_tile(f"{3100 + i}", f"Submitted {i}", TILE_SUFFIX_SUBMITTED))
        for i in range(11):
            tiles.append(_tile(f"{4100 + i}", f"Todo {i}", TILE_SUFFIX_DUE))
        for i in range(6):
            tiles.append(_tile(f"{5100 + i}", f"Unlabelled {i}", TILE_SUFFIX_NOT_ASSESSED))
        return _parse_tiles(client, tiles)

    def test_no_graded_or_submitted_tile_is_reported_unsubmitted(self, client):
        tiles = self._tiles_for(client)
        by_title = {t["title"]: t for t in tiles}

        for name, task in by_title.items():
            if name.startswith("Graded"):
                assert task["grade_letter"] == "D", name
                assert task["status"] is None, name
            if name.startswith("Submitted"):
                assert task["status"] == SUBMISSION_SUBMITTED, name
            if name.startswith("Todo"):
                assert task["status"] == SUBMISSION_NOT_SUBMITTED, name
            if name.startswith("Unlabelled"):
                assert task["status"] is None, name

    def test_todo_count_drops_from_28_to_the_11_due_tiles(self, client):
        tiles = self._tiles_for(client)
        todo = [t for t in tiles if get_task_display_status(t) == "Incomplete (Todo)"]
        assert {t["title"] for t in todo} == {f"Todo {i}" for i in range(11)}


# ── Consequence 1: the classifier tolerates every spelling ───────────────


class TestClassifierToleratesEverySpelling:
    """Defence in depth: even raw page text classifies correctly."""

    @pytest.mark.parametrize(
        "raw",
        ["not-submitted", "not submitted", "Not Submitted", "NOT SUBMITTED", "not_submitted"],
    )
    def test_unsubmitted_spellings_are_pending(self, raw):
        task = {"status": raw, "due_date": "Sep 07, 11:59 PM"}
        assert get_submission_status(task) == SubmissionStatus.PENDING
        assert is_task_todo(task) is True
        assert get_task_display_status(task) == "Incomplete (Todo)"

    @pytest.mark.parametrize("raw", ["submitted", "Submitted", "SUBMITTED"])
    def test_submitted_spellings_are_submitted(self, raw):
        assert get_submission_status({"status": raw}) == SubmissionStatus.SUBMITTED

    def test_a_phrase_that_is_not_purely_a_state_is_not_read_as_one(self):
        """The whitelist is deliberate: an action or a qualifier is not a state.

        "Submit Coursework" is what a live dropbox link says, and reading it as a
        state reported a pending task as already submitted.  The badge's
        ``data-bs-title`` ("18 hours early") is likewise never parsed as a state.
        """
        for raw in ("Submit Coursework", "Upload submission", "Submitted 18 hours early"):
            assert normalize_submission_status(raw) is None, raw

    def test_pending_and_waiting_wordings_are_the_unsubmitted_state(self):
        for raw in ("Pending", "pending", "Waiting", "Not submitted yet", "No submission"):
            assert get_submission_status({"status": raw}) == SubmissionStatus.PENDING, raw

    def test_a_state_free_status_is_not_invented(self):
        for raw in (None, "", "graded", "Not Assessed Yet", "Formative"):
            assert get_submission_status({"status": raw}) == SubmissionStatus.NONE, raw

    def test_normalizer_round_trip(self):
        assert normalize_submission_status("Not Submitted") == SUBMISSION_NOT_SUBMITTED
        assert normalize_submission_status("not-submitted") == SUBMISSION_NOT_SUBMITTED
        assert normalize_submission_status("Submitted") == SUBMISSION_SUBMITTED
        assert normalize_submission_status(None) is None
        assert normalize_submission_status("") is None

    def test_labels_prefer_the_submitted_state(self):
        assert submission_status_from_labels(["Formative", "Submitted"]) == SUBMISSION_SUBMITTED
        assert submission_status_from_labels(["Formative", "Pending"]) == SUBMISSION_NOT_SUBMITTED
        assert submission_status_from_labels(["Formative"]) is None
        assert submission_status_from_labels(None) is None
