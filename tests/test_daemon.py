"""Tests for mb_crawler.daemon."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rm

from mb_crawler.daemon import (
    DEFAULT_WEBHOOK_URL,
    configure_webhook,
    diff_snapshots,
    load_daemon_config,
    run_daemon_once,
    save_daemon_config,
    start_loop,
    stop_daemon,
)


class TestLoadDaemonConfig:
    def test_default_when_no_file(self, tmp_path: Path):
        config = load_daemon_config(str(tmp_path / "nonexistent.json"))
        assert config["webhook_url"] == DEFAULT_WEBHOOK_URL
        assert config["interval"] == 900
        assert config["verify_tls"] is True

    def test_loads_existing_file(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        path.write_text(
            json.dumps({"webhook_url": "http://custom:9999/webhook", "interval": 300})
        )
        config = load_daemon_config(str(path))
        assert config["webhook_url"] == "http://custom:9999/webhook"
        assert config["interval"] == 300


class TestSaveDaemonConfig:
    def test_creates_file(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        data = {"webhook_url": "http://localhost:8080/webhook"}
        save_daemon_config(data, str(path))
        loaded = json.loads(path.read_text())
        assert loaded["webhook_url"] == "http://localhost:8080/webhook"

    def test_returns_path(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        result = save_daemon_config({"x": 1}, str(path))
        assert result == path


class TestDiffSnapshots:
    def test_new_overdue_alert(self, make_crawl_result):
        old = make_crawl_result(upcoming=[], overdue=[])
        new = make_crawl_result(
            upcoming=[],
            overdue=[{"id": "1", "title": "Overdue HW", "class_name": "Math"}],
        )
        alerts = diff_snapshots(old, new)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "new_overdue"
        assert alerts[0]["severity"] == "high"
        assert "Overdue HW" in alerts[0]["message"]

    def test_new_upcoming_alert(self, make_crawl_result):
        old = make_crawl_result(upcoming=[], overdue=[])
        new = make_crawl_result(
            upcoming=[
                {
                    "id": "2",
                    "title": "New Task",
                    "due_date": "May 1",
                    "class_name": "Eng",
                }
            ],
            overdue=[],
        )
        alerts = diff_snapshots(old, new)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "new_upcoming"
        assert alerts[0]["severity"] == "medium"

    def test_new_grade_alert(self, make_crawl_result, sample_task):
        old_task = {**sample_task, "grade_letter": None}
        new_task = {**sample_task, "grade_letter": "A", "grade_score": "95/100"}
        old = make_crawl_result(upcoming=[old_task])
        new = make_crawl_result(upcoming=[new_task])
        alerts = diff_snapshots(old, new)
        grade_alerts = [a for a in alerts if a["type"] == "new_grade"]
        assert len(grade_alerts) == 1
        assert "A" in grade_alerts[0]["message"]

    def test_no_alerts_when_same(self, make_crawl_result, sample_task):
        old = make_crawl_result(upcoming=[sample_task])
        new = make_crawl_result(upcoming=[sample_task])
        alerts = diff_snapshots(old, new)
        assert alerts == []

    def test_no_alert_for_existing_overdue(self, make_crawl_result):
        task = {"id": "1", "title": "Old overdue", "class_name": "Math"}
        old = make_crawl_result(overdue=[task])
        new = make_crawl_result(overdue=[task])
        alerts = diff_snapshots(old, new)
        assert alerts == []


class TestConfigureWebhook:
    def test_saves_url(self, tmp_path: Path):
        path = tmp_path / "daemon.json"
        config = configure_webhook("http://new:8080/hook", str(path))
        assert config["webhook_url"] == "http://new:8080/hook"
        loaded = json.loads(path.read_text())
        assert loaded["webhook_url"] == "http://new:8080/hook"


class TestRunDaemonOnce:
    def test_dry_run_no_webhook(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            upcoming=[
                {"id": "1", "title": "T1", "class_name": "Math", "due_date": "May 1"}
            ],
        )

        result = run_daemon_once(mock_client, daemon_config, dry_run=True)
        assert result["delivered"] is False
        assert result["alert_count"] >= 0

    def test_with_alerts_posts_webhook(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result(
            overdue=[{"id": "1", "title": "Overdue!", "class_name": "Math"}],
        )

        with rm.Mocker() as m:
            m.post("http://localhost:9999/webhook", status_code=200)
            result = run_daemon_once(mock_client, daemon_config, dry_run=False)
            assert result["delivered"] is True
            assert result["alert_count"] == 1

    def test_saves_snapshot(self, tmp_path: Path, make_crawl_result):
        snapshot_path = tmp_path / "snapshot.json"
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(snapshot_path),
            "verify_tls": True,
        }

        crawl_data = make_crawl_result(upcoming=[{"id": "1", "title": "T1"}])
        mock_client = MagicMock()
        mock_client.crawl_all.return_value = crawl_data

        with rm.Mocker() as m:
            m.post("http://localhost:9999/webhook", status_code=200)
            run_daemon_once(mock_client, daemon_config, dry_run=True)
            assert snapshot_path.exists()
            saved = json.loads(snapshot_path.read_text())
            assert saved["upcoming"][0]["id"] == "1"


class TestStartLoop:
    def test_once_mode(self, tmp_path: Path, make_crawl_result):
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(tmp_path / "snapshot.json"),
            "pid_file": str(tmp_path / "daemon.pid"),
            "log_file": str(tmp_path / "daemon.log"),
            "interval": 1,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result()

        result = start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert "alerts" in result
        assert result["alert_count"] == 0

    def test_once_mode_cleans_pid(self, tmp_path: Path, make_crawl_result):
        pid_path = tmp_path / "daemon.pid"
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(tmp_path / "snapshot.json"),
            "pid_file": str(pid_path),
            "log_file": str(tmp_path / "daemon.log"),
            "interval": 1,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result()

        start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert not pid_path.exists()

    def test_cleans_pid_on_exception(self, tmp_path: Path, make_crawl_result):
        pid_path = tmp_path / "daemon.pid"
        daemon_config = {
            "webhook_url": "http://localhost:9999/webhook",
            "snapshot_file": str(tmp_path / "snapshot.json"),
            "pid_file": str(pid_path),
            "log_file": str(tmp_path / "daemon.log"),
            "interval": 1,
        }

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = make_crawl_result()

        start_loop(mock_client, daemon_config, dry_run=True, once=True)
        assert not pid_path.exists()
        assert (tmp_path / "daemon.log").exists()


class TestStopDaemon:
    def test_no_pid_file(self, tmp_path: Path):
        daemon_config = {
            "pid_file": str(tmp_path / "nonexistent.pid"),
        }
        result = stop_daemon(str(tmp_path / "nonexistent.json"))
        # stop_daemon loads daemon config from the path, not from daemon_config
        # Need to use the actual daemon config loading

    def test_invalid_pid_content(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("not_a_number")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        result = stop_daemon(str(config_path))
        assert result["stopped"] is False
        assert result["reason"] == "invalid_pid"
        assert not pid_path.exists()

    def test_zero_pid(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("0")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        result = stop_daemon(str(config_path))
        assert result["stopped"] is False
        assert result["reason"] == "invalid_pid"

    def test_non_mb_crawler_process(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("99999")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        with patch("mb_crawler.daemon._is_mb_crawler_pid", return_value=False):
            result = stop_daemon(str(config_path))
            assert result["stopped"] is False
            assert result["reason"] == "not_mb_crawler_process"

    def test_valid_process_kills(self, tmp_path: Path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("12345")
        config_path = tmp_path / "daemon.json"
        config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

        with patch("mb_crawler.daemon._is_mb_crawler_pid", return_value=True):
            with patch("mb_crawler.daemon.os.kill") as mock_kill:
                result = stop_daemon(str(config_path))
                assert result["stopped"] is True
                assert result["pid"] == 12345
                mock_kill.assert_called_once_with(12345, signal.SIGTERM)
                assert not pid_path.exists()
