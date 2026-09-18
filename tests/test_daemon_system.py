"""Tests for system service manager and process management."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mb_cli.daemon.system import ServiceManager, _is_tahuti_process


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
    with patch("mb_cli.daemon.system._is_tahuti_process", return_value=False):
        res = mgr.stop_background(verify_process=True)
        assert res["stopped"] is False
        assert res["reason"] == "not_mb_cli_process"
        assert not pid_file.exists()


def _ps_output(cmdline: str):
    """Stand-in for the `ps -p <pid> -o command=` result the guard parses."""
    return SimpleNamespace(returncode=0, stdout=cmdline + "\n")


def test_is_tahuti_process_accepts_the_spawned_daemon():
    """`daemon stop` must recognise the daemon `start_background` spawns.

    start_background runs `python -m mb_cli daemon run`, so the module name
    has to stay in the match list — dropping it would make stop refuse to stop
    its own daemon.
    """
    accepted = (
        f"{sys.executable} -m mb_cli daemon run",
        "/opt/homebrew/bin/tahuti daemon run",
        "/opt/mb-tools/.venv/bin/python -m mb_cli daemon run --webhook-url http://x",
        # Pre-rename installs whose pid file is still on disk.
        "/usr/bin/python -m mb_crawler daemon run",
    )
    for cmdline in accepted:
        with patch(
            "mb_cli.daemon.system.subprocess.run", return_value=_ps_output(cmdline)
        ):
            assert _is_tahuti_process(4242) is True, cmdline


def test_is_tahuti_process_rejects_unrelated_processes():
    """The bare `mb` substring is excluded on purpose: `systemd` contains it."""
    rejected = (
        "/lib/systemd/systemd --user",
        "/Applications/Safari.app/Contents/MacOS/Safari",
    )
    for cmdline in rejected:
        with patch(
            "mb_cli.daemon.system.subprocess.run", return_value=_ps_output(cmdline)
        ):
            assert _is_tahuti_process(4242) is False, cmdline

    # No such process: `ps` exits non-zero, so there is nothing to match.
    gone = SimpleNamespace(returncode=1, stdout="")
    with patch("mb_cli.daemon.system.subprocess.run", return_value=gone):
        assert _is_tahuti_process(4242) is False
