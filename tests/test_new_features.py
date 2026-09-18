import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from mb_cli.client import parse_due_date, ManageBacClient
from mb_cli.cache import ResponseCache


def test_parse_due_date_wrapping():
    # Mock current datetime to Dec 28, 2026
    fixed_now = datetime(2026, 12, 28, 12, 0, 0)
    with patch("mb_cli.client.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_now
        mock_datetime.strptime = datetime.strptime

        # Naive parse of "Jan 4" would yield Jan 4, 2026.
        # But Jan 4 is in the future relative to Dec 28, 2026.
        # It should adjust to Jan 4, 2027.
        dt = parse_due_date("Jan 4, 9:30 PM")
        assert dt is not None
        assert dt.year == 2027
        assert dt.month == 1
        assert dt.day == 4
        assert dt.hour == 21
        assert dt.minute == 30

    # Mock current datetime to Jan 4, 2027
    fixed_now = datetime(2027, 1, 4, 12, 0, 0)
    with patch("mb_cli.client.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_now
        mock_datetime.strptime = datetime.strptime

        # Naive parse of "Dec 28" would yield Dec 28, 2027.
        # But Dec 28 is in the past relative to Jan 4, 2027.
        # It should adjust to Dec 28, 2026.
        dt = parse_due_date("Dec 28, 6:00 PM")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 12
        assert dt.day == 28
        assert dt.hour == 18
        assert dt.minute == 0


def test_stale_cache_fallback(tmp_path):
    # Setup cache
    cache = ResponseCache(cache_dir=tmp_path, enabled=True)
    cache.put("https://bj80.managebac.cn/test-fallback", "old cached body", 200)
    cache.invalidate("https://bj80.managebac.cn/test-fallback")

    # The client
    client = ManageBacClient("bj80", domain="managebac.cn", cache=cache)

    # Mock the request call to raise a 404 HTTPError (as if deleted by MB)
    import requests
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.raise_for_status.side_effect = requests.HTTPError("404 Not Found", response=mock_resp)

    with patch.object(client.session, "request", return_value=mock_resp):
        # Even though request failed with 404, it should fall back to the invalidated/stale cache!
        soup = client._get("/test-fallback")
        assert soup.get_text() == "old cached body"


def test_view_submissions():
    from mb_cli.formatters import render_pretty
    from mb_cli.formatters import ok

    payload = ok(
        "view",
        "default",
        {
            "task": {
                "id": "123",
                "title": "Submissions Test Task",
                "class_name": "Math HL",
                "due_date": "May 10",
                "link": "http://x",
            },
            "detail": {
                "description": "Do homework 5",
                "submission": "Submitted: 2 files",
                "attachments": [
                    {
                        "name": "submitted_essay.pdf",
                        "url": "http://x/submitted_essay.pdf",
                        "source": "submission",
                    },
                    {
                        "name": "resource_guide.pdf",
                        "url": "http://x/resource_guide.pdf",
                        "source": "description",
                    }
                ],
            },
        },
    )
    output = render_pretty(payload)
    assert "[submissions]" in output
    assert "Submitted: 2 files" in output
    assert "submitted_essay.pdf" in output
    assert "[attachments]" in output
    assert "resource_guide.pdf" in output


def test_cmd_download(tmp_path):
    """`tahuti download` writes the attachment files and reports them as JSON.

    `tahuti download` used to write files and say nothing on stdout, so
    `--format json` and `--output` had nothing to act on.
    """
    from mb_cli.__main__ import cmd_download

    class Args:
        task_id = "123"
        output_dir = str(tmp_path / "custom_out")
        no_submissions = False
        no_attachments = False
        # Every flag `add_common_auth_flags` puts on the real namespace.
        pages = 10
        output = None
        format = None

    args = Args()

    state = MagicMock()
    state.config_path = tmp_path / "config" / "config.json"
    client = MagicMock()
    client.base = "https://bj80.managebac.cn"

    # Mock snapshot data
    snapshot_path = tmp_path / "config" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps({
        "upcoming": [
            {
                "id": "123",
                "title": "Submissions Test Task",
                "link": "http://x/123",
            }
        ],
        "past": [],
        "overdue": []
    }))

    # Detail response mock
    client.get_task_detail.return_value = {
        "attachments": [
            {
                "name": "res.pdf",
                "url": "https://bj80.managebac.cn/res.pdf",
                "source": "description",
            },
            {
                "name": "essay.pdf",
                "url": "https://bj80.managebac.cn/essay.pdf",
                "source": "submission",
            }
        ]
    }

    # Mock client session get stream download.
    # `cmd_download` calls `session.get(...)` and then enters the *returned*
    # response, because it has to inspect the status and Location of each hop
    # before deciding to follow it — so the mock must return the response
    # directly rather than one whose `__enter__` yields it.
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Real booleans: a MagicMock is truthy, and `cmd_download` reads these to
    # decide whether a hop is a redirect.
    mock_resp.is_redirect = False
    mock_resp.is_permanent_redirect = False
    mock_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
    client.session.get.return_value = mock_resp

    captured: dict = {}
    with patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")), \
         patch("mb_cli.__main__._authenticate_client"), \
         patch("mb_cli.__main__.print_payload", side_effect=lambda p, o, f: captured.update(payload=p, output=o, fmt=f)):

        rc = cmd_download(args)
        assert rc == 0

        # Verify output files
        out_dir = tmp_path / "custom_out"
        assert (out_dir / "res.pdf").exists()
        assert (out_dir / "res.pdf").read_bytes() == b"chunk1chunk2"
        assert (out_dir / "essay.pdf").exists()
        assert (out_dir / "essay.pdf").read_bytes() == b"chunk1chunk2"

        assert captured["payload"]["ok"] is True
        assert captured["payload"]["command"] == "download"
        assert captured["payload"]["data"]["downloaded_count"] == 2
        assert captured["payload"]["data"]["failed_count"] == 0
        assert sorted(d["name"] for d in captured["payload"]["data"]["downloaded"]) == [
            "essay.pdf",
            "res.pdf",
        ]
