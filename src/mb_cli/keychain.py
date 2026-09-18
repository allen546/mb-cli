"""Optional OS-keychain storage for the ManageBac password.

Deliberately dependency-free: this shells out to the credential helper that
already ships with the OS — macOS ``security`` and Linux ``secret-tool`` — so
``tahuti`` gains no runtime dependency, no import cost, and no new failure mode
when the feature is unused.

Opt in per login with ``mb login --keychain``, or globally with
``MB_CRAWLER_KEYCHAIN=1``. When enabled the password goes to the keychain
*instead of* the cleartext ``creds.json``, and ``mb logout`` deletes it.

Limits worth knowing (see SECURITY.md):

- The keychain is unlocked only while you are logged in. On a headless Linux
  box with no secret service, stores fail and we fall back to ``creds.json``.
- macOS ``security`` accepts a secret only as an argument, so the password is
  briefly visible in that short-lived child's ``argv``. The window is
  milliseconds, and the item is then encrypted at rest by the login keychain.
  Linux ``secret-tool`` takes the secret on stdin and has no such exposure.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

#: Env var enabling the keychain for every login.
KEYCHAIN_ENV = "MB_CRAWLER_KEYCHAIN"

#: ``security``/``secret-tool`` service name the item is filed under.
SERVICE = "tahuti"

#: Human-readable label shown in Keychain Access / Secret Service prompts.
LABEL = "ManageBac password (tahuti)"

_TRUE = {"1", "true", "yes", "on"}


def _tool() -> list[str] | None:
    """Return the argv prefix for this platform's credential helper, if any."""
    if sys.platform == "darwin":
        exe = shutil.which("security")
        return [exe] if exe else None
    exe = shutil.which("secret-tool")
    return [exe] if exe else None


def available() -> bool:
    """True when this machine has a supported credential helper on PATH."""
    return _tool() is not None


def enabled(explicit: bool | None = None) -> bool:
    """Whether keychain storage is switched on.

    An explicit ``--keychain`` / ``--no-keychain`` flag wins; otherwise the
    ``MB_CRAWLER_KEYCHAIN`` environment variable decides. A machine without a
    usable helper is never "enabled", so callers can rely on this alone to
    pick a backend.
    """
    if explicit is not None:
        return explicit and available()
    if os.environ.get(KEYCHAIN_ENV, "").strip().lower() in _TRUE:
        return available()
    return False


def _run(argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=15)


def store(account: str, secret: str) -> bool:
    """Store *secret* under *account*. Returns True on success."""
    tool = _tool()
    if tool is None or not account or not secret:
        return False
    try:
        if sys.platform == "darwin":
            # -U replaces an existing item instead of erroring on a re-login.
            proc = _run(
                [
                    *tool,
                    "add-generic-password",
                    "-U",
                    "-s",
                    SERVICE,
                    "-a",
                    account,
                    "-l",
                    LABEL,
                    "-w",
                    secret,
                ]
            )
        else:
            proc = _run(
                [*tool, "store", "--label=" + LABEL, SERVICE, account],
                stdin=secret.encode("utf-8"),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("OS keychain store failed: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "OS keychain store failed (rc=%d): %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:200],
        )
        return False
    return True


def lookup(account: str) -> str | None:
    """Return the stored secret for *account*, or *None* if absent/unavailable."""
    tool = _tool()
    if tool is None or not account:
        return None
    try:
        if sys.platform == "darwin":
            proc = _run(
                [*tool, "find-generic-password", "-s", SERVICE, "-a", account, "-w"]
            )
        else:
            proc = _run([*tool, "lookup", SERVICE, account])
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("OS keychain lookup failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    secret = proc.stdout.decode("utf-8", "replace").strip("\n")
    return secret or None


def delete(account: str) -> bool:
    """Remove the stored secret for *account*. Returns True if one was removed."""
    tool = _tool()
    if tool is None or not account:
        return False
    try:
        if sys.platform == "darwin":
            proc = _run(
                [*tool, "delete-generic-password", "-s", SERVICE, "-a", account]
            )
        else:
            proc = _run([*tool, "clear", SERVICE, account])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("OS keychain delete failed: %s", exc)
        return False
    return proc.returncode == 0
