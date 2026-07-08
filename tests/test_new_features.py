import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from mb_cli.client import parse_due_date, ManageBacClient
from mb_cli.filters import matches_completed
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


def test_matches_completed():
    # Task unfinished (todo): not submitted AND no grade AND has submit button
    t1 = {"labels": ["Pending"], "grade_letter": None, "status": "not-submitted", "has_submit_button": True}
    assert matches_completed(t1, completed=False) is True
    assert matches_completed(t1, completed=True) is False

    # Graded F but no submit button -> completed (closed/offline)
    t2 = {"labels": ["Pending"], "grade_letter": "F", "status": "not-submitted"}
    assert matches_completed(t2, completed=True) is True

    # Graded F and has submit button -> completed (since it has a grade)
    t2_open = {"labels": ["Pending"], "grade_letter": "F", "status": "not-submitted", "has_submit_button": True}
    assert matches_completed(t2_open, completed=True) is True

    # Graded passing -> completed
    t3 = {"labels": ["Pending"], "grade_letter": "A", "status": "not-submitted"}
    assert matches_completed(t3, completed=True) is True

    # Submitted -> completed
    t4 = {"labels": ["Submitted"], "grade_letter": None, "status": "submitted"}
    assert matches_completed(t4, completed=True) is True


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
    from mb_cli.__main__ import cmd_download
    from unittest.mock import MagicMock, patch
    import json

    class Args:
        task_id = "123"
        output_dir = str(tmp_path / "custom_out")
        no_submissions = False
        no_attachments = False

    args = Args()

    state = MagicMock()
    state.config_path = tmp_path / "config" / "config.json"
    client = MagicMock()

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
                "url": "http://x/res.pdf",
                "source": "description",
            },
            {
                "name": "essay.pdf",
                "url": "http://x/essay.pdf",
                "source": "submission",
            }
        ]
    }

    # Mock client session get stream download
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
    client.session.get.return_value.__enter__.return_value = mock_resp

    with patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")), \
         patch("mb_cli.__main__._authenticate_client"):
        
        rc = cmd_download(args)
        assert rc == 0
        
        # Verify output files
        out_dir = tmp_path / "custom_out"
        assert (out_dir / "res.pdf").exists()
        assert (out_dir / "res.pdf").read_bytes() == b"chunk1chunk2"
        assert (out_dir / "essay.pdf").exists()
        assert (out_dir / "essay.pdf").read_bytes() == b"chunk1chunk2"


