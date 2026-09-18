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

# How long `start_background` waits before believing a freshly spawned child is
# alive. Long enough for the interpreter to start and fail on a bad flag, short
# enough that `daemon start -b` still feels immediate.
CHILD_LIVENESS_GRACE_SECONDS = 0.5

# What the `--once` path records in the pid file. It is deliberately not a pid:
# `daemon stop` must never signal the parent shell of a one-shot run, and an
# unparseable pid makes both stop paths decline instead of guessing.
ONCE_PID_SENTINEL = "/dev/null"


def _harden_dir(path: Path) -> None:
    """Best-effort restrict a directory to the current user."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _process_is_defunct(pid: int) -> bool:
    """Whether ``pid`` is a zombie: terminated, waiting to be reaped.

    A zombie still answers ``kill(pid, 0)``, so without this a daemon that has
    already exited looks alive and every wait loop runs to its full timeout.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # The comm field can contain spaces and parentheses; the state is
            # the first field after the closing parenthesis.
            fields = fh.read().rpartition(b")")[2].split()
    except OSError:
        return False
    return bool(fields) and fields[0] in (b"Z", b"X", b"x")


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process.

    Signal 0 performs the permission/existence check without delivering
    anything. ``PermissionError`` means the process exists but belongs to
    another user, which still counts as alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError, TypeError):
        return False
    # If the process is our own child we can settle the question exactly by
    # reaping it. Non-children raise ECHILD, which we fall through from.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError, ValueError):
        pass
    return not _process_is_defunct(pid)


def wait_for_exit(pid: int, timeout: float, poll_seconds: float = 0.05) -> bool:
    """Block until ``pid`` is gone or ``timeout`` elapses; True if it exited."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not pid_alive(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_seconds, remaining))


def terminate_pid(
    pid: int,
    term_timeout: float = 5.0,
    kill_timeout: float = 2.0,
) -> dict[str, Any]:
    """SIGTERM ``pid``, wait, then escalate to SIGKILL if it is still there.

    Returns the *verified* outcome — a caller that reports "stopped" without
    this has only reported "a signal was sent". A daemon wedged in a webhook
    retry is exactly the case where SIGTERM is not enough.
    """
    outcome: dict[str, Any] = {"exited": False, "escalated": False}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        outcome["exited"] = True
        return outcome
    except OSError as exc:
        outcome["error"] = str(exc)
        return outcome

    if wait_for_exit(pid, term_timeout):
        outcome["exited"] = True
        return outcome

    outcome["escalated"] = True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        outcome["exited"] = True
        return outcome
    except OSError as exc:
        outcome["error"] = str(exc)
        return outcome

    outcome["exited"] = wait_for_exit(pid, kill_timeout)
    return outcome


def write_pid_file(path: str | Path, pid: int | str | None = None) -> None:
    """Write ``pid`` to ``path`` with 0600 permissions from birth.

    ``Path.write_text`` creates the file with the process umask (0644) and only
    a later ``chmod`` tightens it, so a crash in between leaves a pid file any
    local user can rewrite. ``os.open`` with an explicit mode has no such
    window.

    ``pid`` defaults to the current process. A string is written verbatim, which
    is how the ``--once`` sentinel gets recorded.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(target.parent)
    cur_pid = os.getpid() if pid is None else pid
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(cur_pid) + "\n")


def read_pid_file(path: str | Path) -> int | None:
    """Return the pid recorded in ``path``, or None if absent/unparseable."""
    target = Path(path).expanduser()
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _is_tahuti_process(pid: int) -> bool:
    """Verify PID corresponds to a tahuti process to prevent terminating recycled PIDs.

    The bare ``"mb"`` substring is deliberately absent: it matches any process
    whose command line merely contains those two letters (``systemd``,
    ``kubelet``, ``Kubernetes``…), which would let a stale PID file cause a
    signal to be delivered to an unrelated process.  Only the full module and
    package names are accepted.

    ``mb_cli`` stays in the list because it is what ``start_background``
    actually spawns (``python -m mb_cli daemon run``) and what the generated
    launchd plist and systemd unit exec — the import path is deliberately not
    renamed.  ``tahuti`` covers a daemon started from the console script;
    ``mb_crawler`` and ``mb.cli`` cover pre-rename installs.
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
            for k in ("tahuti", "mb_cli", "mb_crawler", "mb.cli")
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
        except OSError:
            return None
        try:
            pid = int(raw)
        except ValueError:
            # Unparseable content: clear it, but only if it is still the same
            # unparseable content (a concurrent start would have replaced it).
            self.clean_pid()
            return None
        if pid <= 0:
            self.clean_pid(expected_pid=pid)
            return None
        # `pid_alive` rather than a bare kill(pid, 0): it also recognises a
        # terminated-but-unreaped child, which signal 0 reports as alive.
        return pid if pid_alive(pid) else self._drop_stale(pid)

    def _drop_stale(self, pid: int) -> None:
        """Clear a pid file naming a dead pid, unless it has been replaced."""
        self.clean_pid(expected_pid=pid)
        return None

    def write_pid(self, pid: int | None = None) -> None:
        write_pid_file(self.pid_path, pid)

    def clean_pid(self, expected_pid: int | None = None) -> None:
        """Remove the pid file, but only if it still names ``expected_pid``.

        Without that guard a ``daemon stop`` that finishes after a concurrent
        ``daemon start`` unlinks the *new* daemon's pid file, leaving a live
        process nobody can stop.
        """
        if expected_pid is not None:
            current = read_pid_file(self.pid_path)
            if current is not None and current != expected_pid:
                log.info(
                    "Leaving pid file %s in place: it now names pid %s, not %s",
                    self.pid_path,
                    current,
                    expected_pid,
                )
                return
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

        child_env = None
        if env:
            child_env = os.environ.copy()
            child_env.update(env)

        cmd = [sys.executable, "-m", "mb_cli", "daemon", "run"]
        if extra_args:
            cmd.extend(extra_args)

        try:
            # The fd is handed to the child; this copy must be closed whatever
            # happens below, or every failed start leaks a descriptor.
            log_file = os.fdopen(log_fd, "a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=child_env,
                )
            finally:
                log_file.close()
        except Exception:
            # Popen failed before taking ownership of the fd in some paths; make
            # sure it is not left dangling.
            try:
                os.close(log_fd)
            except OSError:
                pass
            raise

        # A child can die within milliseconds of spawn (bad flag, missing
        # module, unreadable config). Reporting "started" before it is known to
        # be alive is how a stop-then-start script ends up with two daemons
        # fighting over daemon_state.json.
        if not self._child_survived(proc):
            log.error(
                "Daemon child exited immediately (returncode=%s); see %s",
                proc.returncode,
                self.log_path,
            )
            self.clean_pid(expected_pid=proc.pid)
            return {
                "started": False,
                "reason": "child_exited",
                "returncode": proc.returncode,
                "log_file": str(self.log_path),
            }

        self.write_pid(proc.pid)
        return {
            "started": True,
            "pid": proc.pid,
            "log_file": str(self.log_path),
        }

    @staticmethod
    def _child_survived(proc: subprocess.Popen) -> bool:
        """Poll briefly to confirm a spawned child is still running."""
        deadline = time.monotonic() + CHILD_LIVENESS_GRACE_SECONDS
        while True:
            if proc.poll() is not None:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.025, remaining))

    def stop_background(self, verify_process: bool = True) -> dict[str, Any]:
        """Gracefully terminate the background daemon process."""
        pid = self.get_running_pid()
        if not pid:
            self.clean_pid()
            return {"stopped": False, "reason": "not_running"}

        if verify_process and not _is_tahuti_process(pid):
            self.clean_pid(expected_pid=pid)
            return {
                "stopped": False,
                "reason": "not_mb_cli_process",
                "pid": pid,
            }

        outcome = terminate_pid(pid)
        self.clean_pid(expected_pid=pid)
        if outcome.get("exited"):
            return {"stopped": True, "pid": pid}
        return {
            "stopped": False,
            "reason": "did_not_exit",
            "pid": pid,
            "escalated_to_sigkill": outcome.get("escalated", False),
            "error": outcome.get("error"),
        }

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
                "reason": f"Unsupported platform for auto-service: {system}. Use 'tahuti daemon run' or 'tahuti daemon start'.",
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
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.tahuti.daemon.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        python_bin = sys.executable

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
    <string>com.tahuti.daemon</string>
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
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.tahuti.daemon.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            return {"uninstalled": True, "service_file": str(plist_path)}
        return {"uninstalled": False, "reason": "service_file_missing"}

    def _install_linux_systemd(self) -> dict[str, Any]:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / "tahuti-daemon.service"

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
        res = subprocess.run(["systemctl", "--user", "enable", "--now", "tahuti-daemon"], capture_output=True, text=True)
        return {
            "installed": res.returncode == 0,
            "service_file": str(service_path),
            "output": res.stdout + res.stderr,
        }

    def _uninstall_linux_systemd(self) -> dict[str, Any]:
        service_path = Path.home() / ".config" / "systemd" / "user" / "tahuti-daemon.service"
        if service_path.exists():
            subprocess.run(["systemctl", "--user", "disable", "--now", "tahuti-daemon"], capture_output=True)
            service_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return {"uninstalled": True, "service_file": str(service_path)}
        return {"uninstalled": False, "reason": "service_file_missing"}
