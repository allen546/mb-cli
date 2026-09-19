"""Tests for the CLI defects the publish-prep audit found by execution.

Each test names the command it covers and the symptom it used to produce.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from mb_cli import __version__
from mb_cli.__main__ import build_parser, main


# ── `tahuti --version` ───────────────────────────────────────────────────


def test_version_flag_exits_zero_with_package_version(capsys):
    """`tahuti --version` used to die with argparse's exit 2 (required subparsers)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert out.startswith("tahuti ")


def test_version_short_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["-V"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_version_is_sourced_from_mb_cli_version():
    """Guards against the string drifting from `mb_cli.__version__`."""
    parser = build_parser()
    action = next(
        a for a in parser._actions if getattr(a, "dest", None) == "version"
    )
    assert __version__ in action.version


# ── `tahuti submissions --check-feedback <filter>` ───────────────────────


class _SubmissionsArgs:
    def __init__(self, **overrides):
        self.target = "123"
        self.id = None
        self.pages = 10
        self.list = False
        self.add = None
        self.delete = None
        self.check_feedback = None
        self.output = None
        self.format = None
        self.profile = None
        for key, value in overrides.items():
            setattr(self, key, value)


def _feedback_dict(items):
    return {
        "task_id": "123",
        "class_id": "456",
        "task_url": "http://x/123",
        "grade": {"grade_letter": "A", "grade_score": "9/10"},
        "general_comments": ["Nice work"],
        "feedback_items": items,
    }


def _feedback_item(name, comment="ok"):
    return {
        "submission_name": name,
        "feedback_url": None,
        "comment": comment,
        "rubric": [],
        "attachments": [{"name": f"{name}.annotated.pdf"}],
        "annotated_download_url": None,
        "error": None,
    }


def test_check_feedback_with_filter_does_not_crash(capsys):
    """`get_teacher_feedback` returns a dict; iterating it used to raise
    AttributeError: 'str' object has no attribute 'get'."""
    from mb_cli.__main__ import cmd_submissions

    items = [_feedback_item("essay.pdf"), _feedback_item("quiz.pdf")]
    client = MagicMock()
    client.get_teacher_feedback.return_value = _feedback_dict(items)
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__._resolve_task_ids", return_value=("456", "123")),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
    ):
        rc = cmd_submissions(_SubmissionsArgs(check_feedback="essay"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    names = [i["submission_name"] for i in payload["data"]["feedback_items"]]
    assert names == ["essay.pdf"]
    # The non-filtered metadata survives the filter.
    assert payload["data"]["grade"]["grade_letter"] == "A"
    assert payload["data"]["feedback_count"] == 1


def test_check_feedback_without_filter_returns_everything(capsys):
    from mb_cli.__main__ import cmd_submissions

    items = [_feedback_item("essay.pdf"), _feedback_item("quiz.pdf")]
    client = MagicMock()
    client.get_teacher_feedback.return_value = _feedback_dict(items)
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__._resolve_task_ids", return_value=("456", "123")),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
    ):
        rc = cmd_submissions(_SubmissionsArgs(check_feedback=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["data"]["feedback_items"]) == 2


def test_check_feedback_no_match_returns_empty_list_not_original(capsys):
    from mb_cli.__main__ import cmd_submissions

    items = [_feedback_item("quiz.pdf")]
    client = MagicMock()
    client.get_teacher_feedback.return_value = _feedback_dict(items)
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__._resolve_task_ids", return_value=("456", "123")),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
    ):
        rc = cmd_submissions(_SubmissionsArgs(check_feedback="nomatch"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # The old code fell back to the *unfiltered* dict on no match, silently
    # returning feedback the filter was supposed to exclude.
    assert payload["data"]["feedback_items"] == []
    assert payload["data"]["feedback_count"] == 0


# ── `tahuti submit --id` ─────────────────────────────────────────────────


def test_submit_accepts_id_instead_of_positional(capsys):
    from mb_cli.__main__ import cmd_submit

    class Args:
        target = None
        id = "1000026"
        file = "hw.pdf"
        pages = 10
        output = None
        format = None

    client = MagicMock()
    client.submit_file.return_value = {"filename": "hw.pdf", "ok": True}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__._resolve_task_ids", return_value=("456", "1000026")),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
        patch("mb_cli.__main__.find_task_by_id", return_value=None),
    ):
        rc = cmd_submit(Args())

    assert rc == 0
    client.submit_file.assert_called_once_with("456", "1000026", "hw.pdf")


# ── `tahuti view --subject` ──────────────────────────────────────────────


class _ViewArgs:
    def __init__(self, **overrides):
        self.target = None
        self.id = None
        self.url = None
        self.subject = None
        self.pages = 10
        self.refresh = False
        self.output = None
        self.format = None
        for key, value in overrides.items():
            setattr(self, key, value)


def test_view_subject_mismatch_is_reported(capsys):
    from mb_cli.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
        patch("mb_cli.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Physics", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123", subject="Math", format="json"))

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "subject_mismatch"


def test_view_subject_match_passes_through(capsys):
    from mb_cli.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
        patch("mb_cli.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Mathematics HL", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123", subject="Math", format="json"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_view_without_subject_ignores_the_check(capsys):
    from mb_cli.__main__ import cmd_view

    client = MagicMock()
    client.get_task_detail.return_value = {}
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
        patch("mb_cli.__main__.find_task_by_id", return_value={
            "id": "123", "title": "Essay", "class_name": "Physics", "link": "http://x/123"
        }),
    ):
        rc = cmd_view(_ViewArgs(id="123"))

    assert rc == 0


# ── `tahuti notifications --unread-only` ─────────────────────────────────


def test_notifications_unread_only_filters_the_request():
    from mb_cli.__main__ import cmd_notifications

    class Args:
        page = 1
        per_page = 20
        read = None
        unread = None
        read_all = False
        unread_only = True
        output = None
        format = None

    hub = MagicMock()
    hub.stats.return_value = {"unread": 3}
    hub.list.return_value = {"items": [], "meta": {}}
    client = MagicMock()
    client.get_notification_token.return_value = ("https://hub.example", "tok")
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.hub_client", return_value=hub),
    ):
        rc = cmd_notifications(Args())

    assert rc == 0
    hub.list.assert_called_once_with(page=1, per_page=20, filter_="unread")


def test_notifications_defaults_to_all_filter():
    from mb_cli.__main__ import cmd_notifications

    class Args:
        page = 1
        per_page = 20
        read = None
        unread = None
        read_all = False
        unread_only = False
        output = None
        format = None

    hub = MagicMock()
    hub.stats.return_value = {}
    hub.list.return_value = {"items": [], "meta": {}}
    client = MagicMock()
    client.get_notification_token.return_value = ("https://hub.example", "tok")
    state = MagicMock()
    state.active_profile = "default"

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.hub_client", return_value=hub),
    ):
        cmd_notifications(Args())

    hub.list.assert_called_once_with(page=1, per_page=20, filter_="all")


def test_notifications_accepts_unread_only_flag():
    args = build_parser().parse_args(["notifications", "--unread-only"])
    assert args.unread_only is True
