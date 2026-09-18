"""System service installer and process lifecycle management."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from ..config import config_dir

log = logging.getLogger(__name__)

DEFAULT_PID_PATH = config_dir() / "daemon.pid"
DEFAULT_LOG_PATH = config_dir() / "daemon.log"


def _harden_dir(path: Path) -> None:
    """Best-effort restrict a directory to the current user."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _is_mb_cli_process(pid: int) -> bool:
    """Verify PID corresponds to a tahuti process to prevent terminating recycled PIDs.

    The bare ``"mb"`` substring is deliberately absent: it matches any process
    whose command line merely contains those two letters (``systemd``,
    ``kubelet``, ``Kubernetes``…), which would let a stale PID file cause a
    signal to be delivered to an unrelated process.  Only the full module and
    package names are accepted.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        cmdline = result.stdout.strip()
        return any(
            k in cmdline
            for k in ("mb-cli", "mb_cli", "mb_crawler", "mb.cli")
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


class ServiceManager:
    """Manages background daemon processes and OS-level service integrations."""

    def __init__(
        self,
        pid_path: str | Path | None = None,
        log_path: str | Path | None = None,
    ):
        self.pid_path = (
            Path(pid_path).expanduser() if pid_path else DEFAULT_PID_PATH
        )
        self.log_path = (
            Path(log_path).expanduser() if log_path else DEFAULT_LOG_PATH
        )

    # ── PID & Process Management ────────────────────────────────────────

    def get_running_pid(self) -> int | None:
        """Return PID if daemon is currently running, else None."""
        if not self.pid_path.exists():
            return None
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
            pid = int(raw)
            if pid <= 0:
                self.pid_path.unlink(missing_ok=True)
                return None
            # Check process alive
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError):
            self.pid_path.unlink(missing_ok=True)
            return None
        except PermissionError:
            # Running as another user or restricted
            return pid

    def write_pid(self, pid: int | None = None) -> None:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        _harden_dir(self.pid_path.parent)
        cur_pid = pid or os.getpid()
        # Create 0600 so the pid file is never briefly world-writable.
        fd = os.open(str(self.pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(cur_pid) + "\n")

    def clean_pid(self) -> None:
        self.pid_path.unlink(missing_ok=True)

    def start_background(
        self,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start the daemon as a detached background process.

        ``env`` entries are passed to the child through its environment rather
        than argv, so credentials do not appear in ``ps`` output.
        """
        running = self.get_running_pid()
        if running:
            return {
                "started": False,
                "reason": "already_running",
                "pid": running,
            }

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        _harden_dir(self.log_path.parent)
        # Open with 0600 so the log never exists world-readable.
        log_fd = os.open(
            str(self.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.chmod(self.log_path, 0o600)
        except OSError:
            pass
        log_file = os.fdopen(log_fd, "a", encoding="utf-8")

        child_env = None
        if env:
            child_env = os.environ.copy()
            child_env.update(env)

        cmd = [sys.executable, "-m", "mb_cli", "daemon", "run"]
        if extra_args:
            cmd.extend(extra_args)

        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
        self.write_pid(proc.pid)
        return {
            "started": True,
            "pid": proc.pid,
            "log_file": str(self.log_path),
        }

    def stop_background(self, verify_process: bool = True) -> dict[str, Any]:
        """Gracefully terminate the background daemon process."""
        pid = self.get_running_pid()
        if not pid:
            self.clean_pid()
            return {"stopped": False, "reason": "not_running"}

        if verify_process and not _is_mb_cli_process(pid):
            self.clean_pid()
            return {
                "stopped": False,
                "reason": "not_mb_cli_process",
                "pid": pid,
            }

        try:
            os.kill(pid, signal.SIGTERM)
            # Wait up to 5 seconds for termination
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                # Force kill if still alive
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.clean_pid()
            return {"stopped": True, "pid": pid}
        except Exception as exc:
            self.clean_pid()
            return {"stopped": False, "error": str(exc), "pid": pid}

    def status(self) -> dict[str, Any]:
        """Return process health and running status."""
        pid = self.get_running_pid()
        return {
            "running": pid is not None,
            "pid": pid,
            "pid_file": str(self.pid_path),
            "log_file": str(self.log_path),
        }

    # ── OS Service Installation (launchd / systemd) ─────────────────────

    def install_service(self) -> dict[str, Any]:
        """Install user-level background service for current OS."""
        system = platform.system()
        if system == "Darwin":
            return self._install_macos_launchd()
        elif system == "Linux":
            return self._install_linux_systemd()
        else:
            return {
                "installed": False,
                "reason": f"Unsupported platform for auto-service: {system}. Use 'mb daemon run' or 'mb daemon start'.",
            }

    def uninstall_service(self) -> dict[str, Any]:
        """Uninstall user-level background service for current OS."""
        system = platform.system()
        if system == "Darwin":
            return self._uninstall_macos_launchd()
        elif system == "Linux":
            return self._uninstall_linux_systemd()
        else:
            return {
                "uninstalled": False,
                "reason": f"Unsupported platform: {system}",
            }

    def _install_macos_launchd(self) -> dict[str, Any]:
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.managebac.crawler.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        python_bin = sys.executable
        mb_bin = shutil.which("mb") or f"{python_bin} -m mb_cli"

        args_xml = f"""    <string>{python_bin}</string>
    <string>-m</string>
    <string>mb_cli</string>
    <string>daemon</string>
    <string>run</string>"""

        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.managebac.crawler</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{self.log_path}</string>
    <key>StandardErrorPath</key>
    <string>{self.log_path}</string>
</dict>
</plist>
"""
        plist_path.write_text(content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        res = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
        return {
            "installed": res.returncode == 0,
            "service_file": str(plist_path),
            "output": res.stdout + res.stderr,
        }

    def _uninstall_macos_launchd(self) -> dict[str, Any]:
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.managebac.crawler.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            return {"uninstalled": True, "service_file": str(plist_path)}
        return {"uninstalled": False, "reason": "service_file_missing"}

    def _install_linux_systemd(self) -> dict[str, Any]:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / "mb-daemon.service"

        python_bin = sys.executable
        content = f"""[Unit]
Description=ManageBac Notification & DDL Daemon
After=network.target

[Service]
Type=simple
ExecStart={python_bin} -m mb_cli daemon run
Restart=on-failure
RestartSec=30
StandardOutput=append:{self.log_path}
StandardError=append:{self.log_path}

[Install]
WantedBy=default.target
"""
        service_path.write_text(content, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        res = subprocess.run(["systemctl", "--user", "enable", "--now", "mb-daemon"], capture_output=True, text=True)
        return {
            "installed": res.returncode == 0,
            "service_file": str(service_path),
            "output": res.stdout + res.stderr,
        }

    def _uninstall_linux_systemd(self) -> dict[str, Any]:
        service_path = Path.home() / ".config" / "systemd" / "user" / "mb-daemon.service"
        if service_path.exists():
            subprocess.run(["systemctl", "--user", "disable", "--now", "mb-daemon"], capture_output=True)
            service_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return {"uninstalled": True, "service_file": str(service_path)}
        return {"uninstalled": False, "reason": "service_file_missing"}
