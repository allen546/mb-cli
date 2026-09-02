"""Tests for stealth crawler."""

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
    assert task["has_submit_button"] is True

    # Assert that parent class calendar was visited first
    assert mock_client._get.call_count >= 2
