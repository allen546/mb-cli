"""Submission-state parsing, pinned to the frozen (Era 2) behaviour.

Every state-bearing fragment in this file is verbatim markup captured from the
``myschool.managebac.cn`` class-grades and tasks-list pages on 2026-09-19.

**What this file is for.**  A 2026-09-19 review found real defects in how these
signals are read and fixed them, then discovered the fixes *moved
classifications*.  The owner ruled that classification output is frozen — the
frozen version is the one that survived a week of live pressure testing through
the daemon and webhook — so the fixes were reverted here and the corrected
readings were preserved as **additive fields** that no classifier consults.

The contract this file pins:

* ``status`` carries ManageBac's own spelling, verbatim.  The unsubmitted state
  is ``<span class="cell not-submitted">`` whose *text* is "Not Submitted" —
  capital N, capital S, a space where the token has a hyphen.
* :func:`~mb_cli.task_status.get_submission_status` compares ``status`` by
  **exact lowercased string equality**.  So "not submitted" never matches
  "not-submitted", and on the class path PENDING is reached through
  ``has_submit_btn`` alone.  That asymmetry is load-bearing; see §"Why the
  classifier is exact" below.
* The corrected reading lives in ``submission_status`` (class path) and
  ``tile_declared_status`` (tile path).  Neither moves a classification, and
  both exist so a future *approved* rule can be built on real signals.

Changing anything in the first three bullets is a classification change and
needs the owner's approval, not just a passing test run.
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
    classify_task_view,
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


# ── Why the classifier is exact ──────────────────────────────────────────


class TestWhyTheClassifierIsExact:
    """The frozen contract, stated as executable assertions.

    Every test here is a behaviour the review *tried to change* and reverted.
    They read like bugs because on their own terms they are; they are pinned
    because the owner froze the output, and because reverting them is what makes
    the 24 pre-existing tests in ``test_task_status.py`` pass again.
    """

    def test_the_cell_text_is_stored_verbatim(self, client):
        """``status`` keeps the page's spelling, space and all."""
        tasks = _parse_cards(client, [_card("1000103", "Vocabulary quiz", NOT_SUBMITTED_CELL)])
        assert tasks[0]["status"] == "Not Submitted"

    def test_verbatim_cell_text_does_not_match_the_token(self):
        """...which is exactly why it never reaches PENDING through ``status``."""
        task = {"status": "Not Submitted", "due_date": "Sep 07, 11:59 PM"}
        assert get_submission_status(task) == SubmissionStatus.NONE
        assert is_task_todo(task) is False
        assert classify_task_view(task) == "past"

    def test_only_the_exact_token_matches(self):
        assert get_submission_status({"status": "not-submitted"}) == SubmissionStatus.PENDING
        for raw in ("not submitted", "Not Submitted", "NOT SUBMITTED", "not_submitted"):
            assert get_submission_status({"status": raw}) == SubmissionStatus.NONE, raw

    def test_submitted_is_likewise_exact_but_lowercased(self):
        assert get_submission_status({"status": "submitted"}) == SubmissionStatus.SUBMITTED
        assert get_submission_status({"status": "Submitted"}) == SubmissionStatus.SUBMITTED
        assert get_submission_status({"status": "SUBMITTED"}) == SubmissionStatus.SUBMITTED

    def test_has_submit_btn_is_the_class_path_route_to_pending(self, client):
        """Same card, with and without the dropbox link — the only difference."""
        closed = _parse_cards(
            client, [_card("1000106", "Chinese oral", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=False)]
        )[0]
        open_ = _parse_cards(
            client, [_card("1000106", "Chinese oral", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=True)]
        )[0]

        assert closed["status"] == open_["status"] == "Not Submitted"
        assert closed["has_submit_button"] is False
        assert open_["has_submit_button"] is True
        assert get_submission_status(closed) == SubmissionStatus.NONE
        assert get_submission_status(open_) == SubmissionStatus.PENDING

    def test_a_state_free_status_is_not_invented(self):
        for raw in (None, "", "graded", "Not Assessed Yet", "Formative", "Pending", "Waiting"):
            assert get_submission_status({"status": raw}) == SubmissionStatus.NONE, raw


# ── The class path reads only the state-class span and the labels ────────


class TestClassPathReadsOnlyWhatTheFrozenVersionRead:
    """The submitted badge is *not* a signal on the class path — that is the
    single biggest thing the reverted fix changed, so it is pinned hardest.
    """

    def test_no_element_on_a_submitted_card_carries_a_submitted_class(self):
        soup = BeautifulSoup(_card("1000101", "Reading response", SUBMITTED_BADGE), "html.parser")
        assert soup.find(class_=re.compile(r"\bsubmitted\b")) is None, (
            "the live page has no span whose class contains 'submitted' — if this "
            "fixture ever grows one, the fixture stopped being live markup"
        )

    def test_a_bare_submitted_badge_is_not_read_as_submitted(self, client):
        """Frozen behaviour: the badge is not consulted, so the task is not todo.

        ``submission_status`` *does* record the corrected reading — additively,
        without moving the classification.
        """
        tasks = _parse_cards(client, [_card("1000101", "Reading response", SUBMITTED_BADGE)])
        task = tasks[0]

        assert task["labels"] is None
        assert task["status"] is None
        assert task["submission_status"] == SUBMISSION_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.NONE
        assert get_task_display_status(task) == "Complete"

    def test_a_bare_pending_badge_is_not_read_as_unsubmitted(self, client):
        """Same shape, same reason: only its ``badge-label`` says anything."""
        tasks = _parse_cards(client, [_card("1000102", "Essay plan", PENDING_BADGE)])
        task = tasks[0]

        assert task["status"] is None
        assert task["submission_status"] == SUBMISSION_NOT_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.NONE
        assert get_task_display_status(task) == "Complete"

    def test_the_badge_inside_labels_set_is_read_through_the_label(self, client):
        """The one route by which the frozen version *did* detect these.

        When the badge sits inside the card's ``labels-set`` cell its text lands
        in ``labels``, and the frozen label fallback writes the canonical token —
        which then matches exactly.  Detection through a label, never through the
        badge: this asymmetry between the two card shapes is the frozen behaviour.
        """
        cell = f'<div class="cell labels-set">{SUBMITTED_BADGE}</div>'
        tasks = _parse_cards(client, [_card("1000108", "Reading response", cell)])
        task = tasks[0]

        assert "Submitted" in (task["labels"] or [])
        assert task["status"] == SUBMISSION_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.SUBMITTED
        assert get_task_display_status(task) == "Complete (Submitted)"

    def test_a_pending_label_writes_the_canonical_token(self, client):
        """``labels`` spelling the outstanding state does reach PENDING."""
        cell = f'<div class="cell labels-set"><div class="label">Not Submitted</div></div>'
        tasks = _parse_cards(client, [_card("1000109", "Essay plan", cell)])
        task = tasks[0]

        assert task["status"] == SUBMISSION_NOT_SUBMITTED
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
        assert task["submission_status"] is None
        assert get_task_display_status(task) == "Complete (Graded)"


# ── The closed-dropbox case: frozen, and visibly odd ─────────────────────


class TestClosedDropboxStaysComplete:
    """A card the page labels ``Not Submitted`` whose teacher closed the dropbox.

    The review called this the headline defect: with ``has_submit_button`` False
    and the verbatim text not matching, such a task classified as ``none`` and
    displayed as plain "Complete".  That is wrong on its own terms and is exactly
    the §6 divergence — but it is the frozen behaviour, so it is pinned here and
    left alone until the owner approves a change.

    Verified live: none of the 45 class-grades cards is in this shape today, so
    no current task is affected.  Latent, not absent.
    """

    def _closed_dropbox_task(self, client) -> dict:
        tasks = _parse_cards(
            client,
            [_card("1000106", "Chinese oral", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=False)],
        )
        task = tasks[0]
        assert task["has_submit_button"] is False, "this case has no dropbox link"
        return task

    def test_it_is_not_todo(self, client):
        task = self._closed_dropbox_task(client)
        assert task["status"] == "Not Submitted"
        assert task["submission_status"] == SUBMISSION_NOT_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.NONE
        assert is_task_todo(task) is False

    def test_it_displays_complete(self, client):
        assert get_task_display_status(self._closed_dropbox_task(client)) == "Complete"

    def test_it_is_not_unsubmitted_in_the_grade_display(self, client):
        assert format_grade_display(self._closed_dropbox_task(client)) == "Ungraded"

    def test_the_corrected_reading_is_available_anyway(self, client):
        """``submission_status`` carries the state the page actually declared.

        This is the field an approved rule should read; nothing classifies on it
        today, which is precisely why restoring the frozen output was possible
        without discarding the corrected parse.
        """
        assert self._closed_dropbox_task(client)["submission_status"] == SUBMISSION_NOT_SUBMITTED


class TestGetClassTasksGateIsExact:
    """The §6 site: ``get_class_tasks`` reconstructs ``status`` from the card."""

    def _reconstruct(self, client, status: str | None, has_btn: bool) -> dict:
        raw = {
            "task_id": "1000106",
            "title": "Chinese oral",
            "url": f"{client.base}/student/classes/{CLASS_ID}/core_tasks/1000106",
            "due_date": "Sep 07",
            "grade_letter": None,
            "points": None,
            "status": status,
            "category": None,
            "labels": None,
            "has_submit_button": has_btn,
        }
        with patch.object(client, "get_class_grades", return_value={"tasks": [raw]}):
            return client.get_class_tasks(CLASS_ID, class_name="Chinese A", bypass_cache=True)[0]

    def test_verbatim_page_text_does_not_become_the_token(self, client):
        """The reverted fix canonicalised here; the frozen version does not."""
        task = self._reconstruct(client, "Not Submitted", has_btn=False)
        assert task["status"] == "Not Submitted"
        assert task["status"] != SUBMISSION_NOT_SUBMITTED
        assert get_task_display_status(task) == "Complete"

    def test_the_exact_token_still_survives_reconstruction(self, client):
        task = self._reconstruct(client, "not-submitted", has_btn=False)
        assert task["status"] == SUBMISSION_NOT_SUBMITTED
        assert get_submission_status(task) == SubmissionStatus.PENDING
        assert get_task_display_status(task) == "Incomplete (Todo)"

    def test_the_submit_button_alone_is_enough(self, client):
        task = self._reconstruct(client, "Not Submitted", has_btn=True)
        assert task["status"] == SUBMISSION_NOT_SUBMITTED
        assert get_task_display_status(task) == "Incomplete (Todo)"


# ── Genuinely unknown submission state ───────────────────────────────────


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
        assert task["submission_status"] is None
        assert get_submission_status(task) == SubmissionStatus.NONE

    def test_display_string_is_unchanged(self, client):
        tasks = _parse_cards(client, [_card("1000107", "Oral presentation", NOT_ASSESSED_CELL)])
        assert get_task_display_status(tasks[0]) == "Complete"

    def test_not_assessed_yet_is_not_a_submission_state(self):
        assert normalize_submission_status("Not Assessed Yet") is None


# ── The tile path asserts its status ─────────────────────────────────────


class TestTilePathAssertsItsStatus:
    """``_parse_tile`` writes ``status`` unconditionally and never sets
    ``has_submit_button``.

    Both are frozen quirks, and both are pinned:

    * ``status`` is ``"submitted"`` only when the *pre-status* pass proved it;
      everything else is written ``"not-submitted"``.  A ``--submitted`` tile
      therefore reports ``not-submitted`` — the variant is recorded in
      ``tile_declared_status`` but is not fed to the classifier.
    * ``has_submit_button`` is set only when that pass returned PENDING, which
      it never does (it runs before ``status`` exists), so it is always False.
      Measured on 58 live tiles: False in every single case, both versions.

    The downstream consequence — ``submission_status`` reading ``"none"`` beside a
    ``status`` of ``"not-submitted"`` — is the same in both versions and is what
    makes an unsubmitted tile still count as todo.
    """

    def test_submitted_variant_is_not_read_as_submitted(self, client):
        (tile,) = _parse_tiles(client, [_tile("1000099", "Reading log", TILE_SUFFIX_SUBMITTED)])
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["submission_status"] == "none"
        assert tile["tile_declared_status"] == SUBMISSION_SUBMITTED
        assert tile["has_submit_button"] is False
        # The frozen version did not read the suffix body text at all.
        assert tile["grade_letter"] is None
        assert tile["grade_score"] is None

    def test_assessment_variant_parses_the_grade_and_asserts_a_status(self, client):
        (tile,) = _parse_tiles(client, [_tile("1000098", "Unit test", TILE_SUFFIX_ASSESSMENT)])
        assert tile["grade_letter"] == "D"
        assert tile["grade_score"] == "24 /35 pts"
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
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
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["submission_status"] == "none"

    def test_due_variant_is_asserted_but_not_actionable(self, client):
        """No grade element and no text at all — nothing reaches the classifier."""
        (tile,) = _parse_tiles(client, [_tile("1000096", "Essay draft", TILE_SUFFIX_DUE)])
        assert tile["grade_letter"] is None
        assert tile["grade_score"] is None
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["submission_status"] == "none"
        assert tile["tile_declared_status"] == SUBMISSION_NOT_SUBMITTED
        assert tile["has_submit_button"] is False

    def test_every_variant_is_read_from_the_class_alone(self, client):
        """Strip the variant modifier and the declared state disappears with it.

        Proves ``tile_declared_status`` is read from the
        ``f-task-score--<variant>`` class rather than some text heuristic.
        """
        for suffix in (TILE_SUFFIX_SUBMITTED, TILE_SUFFIX_DUE, TILE_SUFFIX_ASSESSMENT):
            stripped = re.sub(r"f-task-score--[a-z-]+", "f-task-score", suffix)
            (tile,) = _parse_tiles(client, [_tile("1000095", "Essay draft", stripped)])
            assert tile["tile_declared_status"] is None, suffix

    def test_legacy_suffix_markup_still_works(self, client):
        """Older tiles with no score box keep their badge/link driven status."""
        legacy_submitted = _tile(
            "1000094",
            "Homework of summer holiday",
            '<div class="f-tile__suffix"><span class="badge">Submitted</span></div>',
        )
        (tile,) = _parse_tiles(client, [legacy_submitted])
        # The badge lands in labels, so the pre-status pass does see it here.
        assert tile["submission_status"] == "submitted"
        assert tile["status"] == SUBMISSION_SUBMITTED

        legacy_pending = _tile(
            "1000093",
            "Poster",
            '<div class="f-tile__suffix"><a class="btn btn-primary" '
            'href="/student/classes/1000012/core_tasks/1000093/dropbox">Submit Coursework</a></div>',
        )
        (tile,) = _parse_tiles(client, [legacy_pending])
        assert tile["has_submit_button"] is True
        assert tile["submission_status"] == "pending"
        assert tile["status"] == SUBMISSION_NOT_SUBMITTED

    def test_a_task_list_tile_is_no_longer_reported_submitted_by_its_action_label(self, client):
        """"Submit Coursework" is an action, not a state."""
        assert submission_status_from_labels(["Formative", "Submit Coursework"]) is None
        assert submission_status_from_labels(["Formative", "Upload submission"]) is None


# ── The frozen tables ────────────────────────────────────────────────────


class TestFrozenClassGradesTable:
    """The 45 live tasks, re-rendered from captured markup.

    Frozen output: 17 Complete (Graded), 11 Complete, 11 Incomplete (Todo),
    6 Complete.  Note the two "Complete" groups — the 11 submitted-badge tasks
    land in the *same* bucket as the 6 unlabelled ones, because the frozen class
    parser reads neither badge.  That is the frozen behaviour, not an oversight
    this file is willing to paper over.
    """

    CARDS: list[str] = []
    EXPECTED: dict[str, str] = {}
    for _i in range(17):
        CARDS.append(_card(f"{2100 + _i}", f"Graded {_i}", GRADED_CELL))
        EXPECTED[f"Graded {_i}"] = "Complete (Graded)"
    # Titles deliberately avoid the substring "submit": the frozen
    # `has_submit_btn` scan accepts *any* <a>/<button> whose text contains it,
    # including the card's own title link.  A task actually named "Submitted…"
    # would therefore be read as actionable.  That is frozen behaviour and a real
    # latent quirk, but it is not what these fixtures are here to measure.
    for _i in range(11):
        CARDS.append(_card(f"{3100 + _i}", f"Handed in {_i}", SUBMITTED_BADGE))
        EXPECTED[f"Handed in {_i}"] = "Complete"
    for _i in range(11):
        CARDS.append(
            _card(f"{4100 + _i}", f"Todo {_i}", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=True)
        )
        EXPECTED[f"Todo {_i}"] = "Incomplete (Todo)"
    for _i in range(6):
        CARDS.append(_card(f"{5100 + _i}", f"Unlabelled {_i}", NOT_ASSESSED_CELL))
        EXPECTED[f"Unlabelled {_i}"] = "Complete"
    # The latent closed-dropbox case, which also lands in "Complete".
    CARDS.append(_card("6100", "Closed dropbox", NOT_SUBMITTED_CELL, PENDING_BADGE, dropbox=False))
    EXPECTED["Closed dropbox"] = "Complete"

    def test_the_45_verified_rows_stay_where_they_are(self, client):
        tasks = _parse_cards(client, self.CARDS)
        assert len(tasks) == 46  # the 45 real tasks plus the latent closed-dropbox case

        counts = Counter(get_task_display_status(t) for t in tasks)
        assert counts["Complete (Graded)"] == 17
        assert counts["Incomplete (Todo)"] == 11
        assert counts["Complete"] == 18  # 11 submitted-badge + 6 unlabelled + 1 closed-dropbox
        assert counts["Complete (Submitted)"] == 0

    def test_every_row_lands_in_its_verified_bucket(self, client):
        tasks = _parse_cards(client, self.CARDS)
        actual = {t["title"]: get_task_display_status(t) for t in tasks}
        assert actual == self.EXPECTED

    def test_the_corrected_readings_are_all_available(self, client):
        """``submission_status`` is right even where ``status`` is frozen dumb."""
        tasks = {t["title"]: t for t in _parse_cards(client, self.CARDS)}

        assert {
            t["submission_status"] for name, t in tasks.items() if name.startswith("Handed")
        } == {SUBMISSION_SUBMITTED}
        assert {
            t["submission_status"] for name, t in tasks.items() if name.startswith("Todo")
        } == {SUBMISSION_NOT_SUBMITTED}
        # Graded and unlabelled cards say nothing about submission; that stays
        # None rather than being coerced into a state.
        assert {
            t["submission_status"] for name, t in tasks.items()
            if name.startswith(("Graded", "Unlabelled"))
        } == {None}


class TestFrozenTileTable:
    """The same 45 tasks as tasks-list tiles.

    Frozen output: 28 Incomplete (Todo) — the 11 submitted-badge tiles, the 11
    due tiles and the 6 not-assessed tiles, all of them, because every tile
    carries an asserted ``status`` of ``not-submitted`` and the downstream
    classifier reads that as PENDING.  Only the 17 graded tiles escape.

    A 2026-09-19 fix narrowed this to the 11 due tiles and moved 17 tasks out of
    todo.  That movement was **not approved** and is reverted; this test is the
    guard that it does not come back by accident.
    """

    def _tiles_for(self, client):
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

    def test_every_tile_asserts_a_status(self, client):
        tiles = self._tiles_for(client)
        assert {t["status"] for t in tiles} == {SUBMISSION_NOT_SUBMITTED}
        assert all(t["has_submit_button"] is False for t in tiles)

    def test_todo_count_is_the_frozen_28(self, client):
        tiles = self._tiles_for(client)
        todo = [t for t in tiles if get_task_display_status(t) == "Incomplete (Todo)"]
        assert len(todo) == 28
        assert {t["title"] for t in todo} == (
            {f"Submitted {i}" for i in range(11)}
            | {f"Todo {i}" for i in range(11)}
            | {f"Unlabelled {i}" for i in range(6)}
        )

    def test_graded_tiles_are_graded(self, client):
        tiles = {t["title"]: t for t in self._tiles_for(client)}
        assert {t["grade_letter"] for name, t in tiles.items() if name.startswith("Graded")} == {"D"}
        assert {
            get_task_display_status(t) for name, t in tiles.items() if name.startswith("Graded")
        } == {"Complete (Graded)"}

    def test_the_declared_variants_are_all_recorded(self, client):
        """The corrected reading, available but inert."""
        tiles = {t["title"]: t for t in self._tiles_for(client)}
        assert {
            t["tile_declared_status"] for name, t in tiles.items() if name.startswith("Submitted")
        } == {SUBMISSION_SUBMITTED}
        assert {
            t["tile_declared_status"] for name, t in tiles.items() if name.startswith("Todo")
        } == {SUBMISSION_NOT_SUBMITTED}
        assert {
            t["tile_declared_status"] for name, t in tiles.items() if name.startswith("Unlabelled")
        } == {None}


# ── The normalizer, which the parse layer still uses ─────────────────────


class TestNormalizer:
    """:func:`normalize_submission_status` survived the revert intact.

    The classifier no longer consults it — that is the whole point of the
    freeze — but the parse layer still calls it to fill the additive
    ``submission_status`` / ``tile_declared_status`` fields, so its contract is
    still worth pinning.
    """

    def test_round_trip(self):
        assert normalize_submission_status("Not Submitted") == SUBMISSION_NOT_SUBMITTED
        assert normalize_submission_status("not-submitted") == SUBMISSION_NOT_SUBMITTED
        assert normalize_submission_status("Submitted") == SUBMISSION_SUBMITTED
        assert normalize_submission_status(None) is None
        assert normalize_submission_status("") is None

    def test_a_phrase_that_is_not_purely_a_state_is_not_read_as_one(self):
        """The whitelist is deliberate: an action or a qualifier is not a state.

        "Submit Coursework" is what a live dropbox link says, and reading it as a
        state reported a pending task as already submitted.  The badge's
        ``data-bs-title`` ("18 hours early") is likewise never parsed as a state.
        """
        for raw in ("Submit Coursework", "Upload submission", "Submitted 18 hours early"):
            assert normalize_submission_status(raw) is None, raw

    def test_labels_prefer_the_submitted_state(self):
        assert submission_status_from_labels(["Formative", "Submitted"]) == SUBMISSION_SUBMITTED
        assert submission_status_from_labels(["Formative", "Pending"]) == SUBMISSION_NOT_SUBMITTED
        assert submission_status_from_labels(["Formative"]) is None
        assert submission_status_from_labels(None) is None
