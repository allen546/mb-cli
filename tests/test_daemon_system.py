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
