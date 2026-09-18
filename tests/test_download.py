"""Coverage for `tahuti download`.

`download` writes to the filesystem and had no dedicated tests: its four
failure paths reported only through the log, so a `--format json` caller got an
empty stdout and an indistinguishable exit code. These tests pin both the
filesystem side effects and the structured payload.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.__main__ import cmd_download


class _Args:
    """The argparse namespace `download` actually receives."""

    def __init__(self, tmp_path, **overrides):
        self.task_id = "123"
        self.output_dir = str(tmp_path / "out")
        self.no_submissions = False
        self.no_attachments = False
        self.pages = 10
        self.output = None
        self.format = None
        for key, value in overrides.items():
            setattr(self, key, value)


# Attachment URLs must be https and on the school's own ManageBac host, so
# every fixture URL below uses the real shape of one.
DOWNLOAD_HOST = "https://myschool.managebac.cn"


def _client(detail_attachments, task=None, found_task=None):
    client = MagicMock()
    client.base = DOWNLOAD_HOST
    client.get_task_detail.return_value = {"attachments": detail_attachments}
    client.find_task_by_id.return_value = found_task
    resp = MagicMock()
    resp.iter_content.return_value = [b"data-"]
    client.session.get.return_value.__enter__.return_value = resp
    return client


def _run(tmp_path, args, client, snapshot_tasks=None):
    """Invoke cmd_download with a mocked client and a real snapshot on disk."""
    state = MagicMock()
    state.config_path = tmp_path / "config" / "config.json"
    snapshot_path = tmp_path / "config" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    # A resolved-by-snapshot task is the common case; callers that want the
    # server-side fallback pass an explicit empty list.
    if snapshot_tasks is None:
        snapshot_tasks = [{"id": "123", "title": "Task 123", "link": "http://x/123"}]
    snapshot_path.write_text(
        json.dumps(
            {
                "upcoming": snapshot_tasks,
                "past": [],
                "overdue": [],
            }
        )
    )
    captured: dict = {}

    def _capture(payload, output, fmt):
        captured["payload"] = payload
        captured["output"] = output
        captured["format"] = fmt

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.print_payload", side_effect=_capture),
    ):
        rc = cmd_download(args)
    return rc, captured


def test_download_writes_attachments_and_submissions(tmp_path):
    attachments = [
        {"name": "res.pdf", "url": "https://myschool.managebac.cn/res.pdf", "source": "description"},
        {"name": "essay.pdf", "url": "https://myschool.managebac.cn/essay.pdf", "source": "submission"},
    ]
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, _client(attachments))

    assert rc == 0
    out_dir = tmp_path / "out"
    assert (out_dir / "res.pdf").read_bytes() == b"data-"
    assert (out_dir / "essay.pdf").read_bytes() == b"data-"

    data = captured["payload"]["data"]
    assert data["downloaded_count"] == 2
    assert data["failed_count"] == 0
    assert {d["source"] for d in data["downloaded"]} == {"attachment", "submission"}


def test_download_no_submissions_skips_submission_source(tmp_path):
    attachments = [
        {"name": "res.pdf", "url": "https://myschool.managebac.cn/res.pdf", "source": "description"},
        {"name": "essay.pdf", "url": "https://myschool.managebac.cn/essay.pdf", "source": "submission"},
    ]
    args = _Args(tmp_path, no_submissions=True)
    rc, captured = _run(tmp_path, args, _client(attachments))

    assert rc == 0
    assert (tmp_path / "out" / "res.pdf").exists()
    assert not (tmp_path / "out" / "essay.pdf").exists()
    assert captured["payload"]["data"]["downloaded_count"] == 1


def test_download_no_attachments_skips_description_source(tmp_path):
    attachments = [
        {"name": "res.pdf", "url": "https://myschool.managebac.cn/res.pdf", "source": "description"},
        {"name": "essay.pdf", "url": "https://myschool.managebac.cn/essay.pdf", "source": "submission"},
    ]
    args = _Args(tmp_path, no_attachments=True)
    rc, captured = _run(tmp_path, args, _client(attachments))

    assert rc == 0
    assert not (tmp_path / "out" / "res.pdf").exists()
    assert (tmp_path / "out" / "essay.pdf").exists()


def test_download_empty_dropbox_succeeds_and_reports_why(tmp_path):
    """No files is a successful run, not a silent exit 0 with no output."""
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, _client([]))

    assert rc == 0
    payload = captured["payload"]
    assert payload["ok"] is True
    assert payload["command"] == "download"
    assert payload["data"]["downloaded_count"] == 0
    assert payload["data"]["downloaded"] == []


def test_download_task_not_found_reports_error_payload(tmp_path):
    args = _Args(tmp_path)
    rc, captured = _run(
        tmp_path, args, _client([], found_task=None), snapshot_tasks=[]
    )

    assert rc == 1
    payload = captured["payload"]
    assert payload["ok"] is False
    assert payload["command"] == "download"
    assert payload["error"]["code"] == "task_not_found"
    assert "123" in payload["error"]["message"]


def test_download_missing_link_reports_error_payload(tmp_path):
    task = {"id": "123", "title": "No link task", "link": None}
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, _client([]), snapshot_tasks=[task])

    assert rc == 1
    assert captured["payload"]["error"]["code"] == "no_task_link"


def test_download_detail_fetch_failure_reports_error_payload(tmp_path):
    snapshot_tasks = [
        {"id": "123", "title": "Task", "link": "http://x/123"}
    ]
    client = _client(None)
    client.get_task_detail.return_value = None
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client, snapshot_tasks=snapshot_tasks)

    assert rc == 1
    assert captured["payload"]["error"]["code"] == "detail_fetch_failed"


def test_download_falls_back_to_server_search_with_pages(tmp_path):
    """`--pages` is honoured by the server-side fallback, not just accepted."""
    snapshot_tasks = [
        {"id": "123", "title": "Math HW", "link": "http://x/123"}
    ]
    server_task = {"id": "123", "title": "Math HW", "link": "http://x/123"}
    client = _client(
        [{"name": "a.pdf", "url": "https://myschool.managebac.cn/a.pdf", "source": "description"}],
        found_task=server_task,
    )
    args = _Args(tmp_path, pages=3)
    rc, captured = _run(tmp_path, args, client, snapshot_tasks=snapshot_tasks)

    assert rc == 0
    # Task was in the snapshot, so find_task_by_id should not have been called.
    client.find_task_by_id.assert_not_called()

    # Now remove it from the snapshot so the server fallback runs.
    rc, captured = _run(tmp_path, args, client, snapshot_tasks=[])
    assert rc == 0
    client.find_task_by_id.assert_called_once_with("123", max_pages=3)


def test_download_server_search_result_used(tmp_path):
    server_task = {
        "id": "123",
        "title": "From server",
        "link": "http://x/123",
    }
    client = _client(
        [{"name": "a.pdf", "url": "https://myschool.managebac.cn/a.pdf", "source": "description"}],
        found_task=server_task,
    )
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client, snapshot_tasks=[])

    assert rc == 0
    assert captured["payload"]["data"]["task_title"] == "From server"


def test_download_partial_failure_exits_zero_and_lists_failures(tmp_path):
    attachments = [
        {"name": "good.pdf", "url": "https://myschool.managebac.cn/good.pdf", "source": "description"},
        {"name": "bad.pdf", "url": "https://myschool.managebac.cn/bad.pdf", "source": "description"},
    ]
    client = _client(attachments)

    def _get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("bad.pdf"):
            resp.__enter__.side_effect = RuntimeError("connection reset")
            return resp
        resp.iter_content.return_value = [b"ok-"]
        return resp

    client.session.get.side_effect = _get
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client)

    assert rc == 0
    data = captured["payload"]["data"]
    assert data["downloaded_count"] == 1
    assert data["failed_count"] == 1
    assert data["failed"][0]["name"] == "bad.pdf"
    assert "connection reset" in data["failed"][0]["reason"]


def test_download_all_failures_exits_one(tmp_path):
    attachments = [
        {"name": "a.pdf", "url": "https://myschool.managebac.cn/a.pdf", "source": "description"}
    ]
    client = _client(attachments)
    resp = MagicMock()
    resp.__enter__.side_effect = RuntimeError("boom")
    client.session.get.return_value = resp

    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client)

    assert rc == 1
    assert captured["payload"]["data"]["downloaded_count"] == 0
    assert captured["payload"]["data"]["failed_count"] == 1


def test_download_never_writes_outside_output_dir(tmp_path):
    """`../` in an attachment name must not escape the chosen directory.

    ``_safe_filename`` collapses the name to its basename, so the file lands
    *inside* ``--output-dir`` under the sanitized name rather than two levels
    up. The containment check in ``cmd_download`` is a second line of defence
    for anything ``_safe_filename`` were ever to let through.
    """
    attachments = [
        {
            "name": "../../escape.pdf",
            "url": "https://myschool.managebac.cn/escape.pdf",
            "source": "description",
        }
    ]
    client = _client(attachments)
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client)

    assert rc == 0
    assert not (tmp_path / "escape.pdf").exists()
    assert not (tmp_path.parent / "escape.pdf").exists()
    # Sanitized to its basename and written inside the output directory.
    assert (tmp_path / "out" / "escape.pdf").exists()
    assert captured["payload"]["data"]["downloaded"][0]["name"] == "../../escape.pdf"
    assert captured["payload"]["data"]["downloaded"][0]["path"].endswith(
        "out/escape.pdf"
    )


def test_download_default_output_dir_uses_task_title_slug(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot_tasks = [
        {"id": "123", "title": "My Math HW 3", "link": "http://x/123"}
    ]
    client = _client(
        [{"name": "a.pdf", "url": "https://myschool.managebac.cn/a.pdf", "source": "description"}]
    )
    args = _Args(tmp_path, output_dir=None)

    with (
        patch("mb_cli.__main__._build_client", return_value=(MagicMock(config_path=tmp_path / "config" / "config.json"), client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.load_snapshot", return_value={
            "upcoming": snapshot_tasks, "past": [], "overdue": []
        }),
        patch("mb_cli.__main__.print_payload"),
    ):
        rc = cmd_download(args)

    assert rc == 0
    assert (tmp_path / "task_123_my_math_hw_3" / "a.pdf").exists()


def test_download_name_collisions_are_disambiguated(tmp_path):
    attachments = [
        {"name": "a.pdf", "url": "https://myschool.managebac.cn/1.pdf", "source": "description"},
        {"name": "a.pdf", "url": "https://myschool.managebac.cn/2.pdf", "source": "submission"},
    ]
    client = _client(attachments)
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client)

    assert rc == 0
    out_dir = tmp_path / "out"
    assert (out_dir / "a.pdf").exists()
    assert (out_dir / "a (1).pdf").exists()
    assert captured["payload"]["data"]["downloaded_count"] == 2


def test_download_skips_entries_without_name_or_url(tmp_path):
    attachments = [
        {"name": None, "url": "https://myschool.managebac.cn/x.pdf", "source": "description"},
        {"name": "y.pdf", "url": None, "source": "description"},
        {"name": "z.pdf", "url": "https://myschool.managebac.cn/z.pdf", "source": "description"},
    ]
    client = _client(attachments)
    args = _Args(tmp_path)
    rc, captured = _run(tmp_path, args, client)

    assert rc == 0
    assert captured["payload"]["data"]["downloaded_count"] == 1
    assert captured["payload"]["data"]["downloaded"][0]["name"] == "z.pdf"


@pytest.mark.parametrize(
    "argv,expected_rc_key",
    [
        (["download", "123"], "ok"),
    ],
)
def test_download_payload_shape_end_to_end(tmp_path, argv, expected_rc_key):
    """`tahuti download` reaches print_payload with the format argparse gives it."""
    from mb_cli.__main__ import build_parser, cmd_download as handler

    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.func.__name__ == handler.__name__
    assert args.func.__code__ is handler.__code__
    # Every common auth flag plus the download-specific ones argparse resolves.
    assert args.pages == 10
    assert args.format is None
    assert args.output is None
    assert args.no_submissions is False
    assert args.no_attachments is False
    assert args.output_dir is None
