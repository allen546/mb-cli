"""Tests for stealth crawler."""

from pathlib import Path
import sys
import importlib

worktree_src = str(Path(__file__).resolve().parent.parent / "src")
if sys.path[0] != worktree_src:
    sys.path.insert(0, worktree_src)

import mb_cli

mb_cli_pkg_dir = str(Path(worktree_src) / "mb_cli")
if hasattr(mb_cli, "__path__") and mb_cli_pkg_dir not in mb_cli.__path__:
    mb_cli.__path__.insert(0, mb_cli_pkg_dir)

try:
    import mb_cli.daemon
    daemon_pkg_dir = str(Path(worktree_src) / "mb_cli" / "daemon")
    if hasattr(mb_cli.daemon, "__path__") and daemon_pkg_dir not in mb_cli.daemon.__path__:
        mb_cli.daemon.__path__.insert(0, daemon_pkg_dir)
except ImportError:
    pass

if "mb_cli.daemon.stealth" in sys.modules:
    importlib.reload(sys.modules["mb_cli.daemon.stealth"])


from unittest.mock import MagicMock
from bs4 import BeautifulSoup
from mb_cli.daemon.stealth import StealthTaskCrawler
from mb_cli.daemon.events import StealthConfig


def test_stealth_crawler_navigates_parent_and_parses():
    mock_client = MagicMock()
    mock_client.base = "https://school.managebac.cn"

    sample_html = """
    <html>
      <body>
        <h3 class="title">Lab Report 1</h3>
        <a href="/student/classes/1001">Physics HL</a>
        <p>Due: September 15, 2026 at 23:59</p>
        <a href="/student/classes/1001/core_tasks/2001/dropbox">Upload</a>
      </body>
    </html>
    """
    mock_client._get.return_value = BeautifulSoup(sample_html, "html.parser")

    crawler = StealthTaskCrawler(
        mock_client, StealthConfig(enabled=True, min_jitter_seconds=0, max_jitter_seconds=0)
    )

    task = crawler.fetch_task_details(class_id=1001, task_id=2001)
    assert task is not None
    assert task["title"] == "Lab Report 1"
    assert task["class_id"] == "1001"
    assert task["task_id"] == "2001"
    assert task["status"] == "not-submitted"
    assert task["due_date"] == "September 15, 2026 at 23:59"
    assert task["has_submit_button"] is True

    # Assert that parent class calendar was visited first
    assert mock_client._get.call_count >= 2


def test_stealth_crawler_badge_handling():
    mock_client = MagicMock()
    mock_client.base = "https://school.managebac.cn"

    # Test "Not Submitted" badge is not mistakenly treated as "submitted"
    not_sub_html = """
    <html>
      <body>
        <h3 class="title">HW 2</h3>
        <span class="badge-status">Not Submitted</span>
        <p>Due: Sep 20, 2026 at 11:59 PM</p>
      </body>
    </html>
    """
    mock_client._get.return_value = BeautifulSoup(not_sub_html, "html.parser")
    crawler = StealthTaskCrawler(
        mock_client, StealthConfig(enabled=False)
    )
    task = crawler.fetch_task_details(class_id=1001, task_id=2002)
    assert task["status"] == "not-submitted"

    # Test "Submitted" badge is recognized
    sub_html = """
    <html>
      <body>
        <h3 class="title">HW 2</h3>
        <span class="badge-status">Submitted</span>
        <p>Due: Sep 20, 2026 at 11:59 PM</p>
      </body>
    </html>
    """
    mock_client._get.return_value = BeautifulSoup(sub_html, "html.parser")
    task_sub = crawler.fetch_task_details(class_id=1001, task_id=2003)
    assert task_sub["status"] == "submitted"


def test_stealth_crawler_uses_client_get_submissions():
    mock_client = MagicMock()
    mock_client.base = "https://school.managebac.cn"
    sample_html = """
    <html><body>
      <h3 class="title">Essay</h3>
      <a href="/student/classes/101">Class</a>
      <a href="/student/classes/101/core_tasks/202/dropbox">Dropbox</a>
    </body></html>
    """
    mock_client._get.return_value = BeautifulSoup(sample_html, "html.parser")
    mock_client.get_submissions.return_value = [{"name": "essay.pdf", "url": "/attachments/1"}]

    crawler = StealthTaskCrawler(mock_client, StealthConfig(enabled=False))
    task = crawler.fetch_task_details(class_id=101, task_id=202)
    assert task["status"] == "submitted"
    mock_client.get_submissions.assert_called_once_with("101", "202")

