"""Tests for system service manager and process management."""

import os
from pathlib import Path
from unittest.mock import patch
from mb_cli.daemon.system import ServiceManager


def test_service_manager_pid_lifecycle(tmp_path: Path):
    pid_file = tmp_path / "daemon.pid"
    mgr = ServiceManager(pid_path=pid_file)

    assert mgr.get_running_pid() is None

    mgr.write_pid(os.getpid())
    assert mgr.get_running_pid() == os.getpid()

    status = mgr.status()
    assert status["running"] is True
    assert status["pid"] == os.getpid()

    mgr.clean_pid()
    assert mgr.get_running_pid() is None


def test_service_manager_stop_unrelated_process(tmp_path: Path):
    pid_file = tmp_path / "daemon.pid"
    mgr = ServiceManager(pid_path=pid_file)

    mgr.write_pid(os.getpid())
    with patch("mb_cli.daemon.system._is_mb_cli_process", return_value=False):
        res = mgr.stop_background(verify_process=True)
        assert res["stopped"] is False
        assert res["reason"] == "not_mb_cli_process"
        assert not pid_file.exists()
