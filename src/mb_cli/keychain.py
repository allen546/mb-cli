"""Optional OS-keychain storage for the ManageBac password.

Deliberately dependency-free: this shells out to the credential helper that
already ships with the OS, so ``tahuti`` gains no runtime dependency, no import
cost, and no new failure mode when the feature is unused. Three platforms:

- **macOS** — the ``security`` binary. The item is encrypted at rest by the
  login keychain.
- **Linux** — ``secret-tool``, talking to the Freedesktop Secret Service.
  Absent on a headless box with no secret service.
- **Windows** — ``powershell.exe`` (Windows PowerShell 5.1, on every stock
  install) driving the WinRT ``PasswordVault``. No module to install and no
  ``cmdkey``-only store.

Opt in per login with ``tahuti login --keychain``, or globally with
``MANAGEBAC_KEYCHAIN=1`` (the pre-rename ``MB_CRAWLER_KEYCHAIN=1`` still works).
When enabled the password goes to the keychain
*instead of* the cleartext ``creds.json``, and ``tahuti logout`` deletes it.

Limits worth knowing (see SECURITY.md):

- The keychain is unlocked only while you are logged in. On a headless Linux
  box with no secret service, stores fail and we fall back to ``creds.json``.
- macOS ``security`` accepts a secret only as an argument, so the password is
  briefly visible in that short-lived child's ``argv``. The window is
  milliseconds, and the item is then encrypted at rest by the login keychain.
  Linux ``secret-tool`` takes the secret on stdin and has no such exposure.
  Feeding ``security`` the password on stdin instead was considered and
  rejected: with ``-w`` omitted it does not read the pipe, it prompts via
  ``readpassphrase(3)``, which reads ``/dev/tty`` whenever a controlling
  terminal exists. That is precisely the interactive ``tahuti login`` case, so
  the stdin variant would appear to work under a daemon and silently prompt (or
  hang) for a human. Removing the exposure needs a different mechanism — the
  Security framework's ``SecItemAdd`` via :mod:`ctypes`, or ``security import``
  with a temporary keychain — not a flag change.
- Windows keeps the secret out of ``argv`` *and* out of the child's
  environment: it is written to ``powershell``'s stdin as raw UTF-8 bytes that
  PowerShell never parses, and a retrieved password comes back base64-encoded
  so PowerShell's output formatting cannot alter it. The flip side is that this
  is the one path with no in-repo test coverage on a real Windows box — and,
  more to the point, *every* path here is one child process away from a
  "success" that persisted nothing. So :func:`store` reads the item straight
  back on every platform and reports failure unless it comes back byte for
  byte: callers unlink the cleartext ``creds.json`` on a ``True`` return, and a
  locked Secret Service collection that lets ``secret-tool`` exit 0 without
  storing must cost the user a fallback to the file, not the password itself.
- Windows is the weakest of the three on two counts, both worth knowing before
  you opt in. ``PasswordVault`` is a WinRT type that .NET Framework projects
  and .NET Core does not, so PowerShell 7 (``pwsh``) typically cannot load it at
  all — 5.1 is preferred and 7 is only a fallback that may fail loudly. And
  Microsoft documents ``PasswordVault`` for the Windows 10 device family with
  no Windows Server entry, so Server support is unverified. A stricter
  alternative exists — ``CredReadW``/``CredWriteW`` on ``advapi32`` via
  ``ctypes``, the same store ``cmdkey`` writes and Control Panel shows — which
  needs no child process and does not roam; it was not chosen here only because
  its struct layout cannot be exercised without a Windows machine.
- **Windows credentials may leave the machine.** Credential Locker roams
  entries to the user's Microsoft account by default and there is no flag to
  turn that off, so a ManageBac password stored this way can be synced off-box.
  If that is unacceptable, do not pass ``--keychain`` on Windows; ``creds.json``
  at ``0600`` at least stays local.
- A keychain item is not covered by your normal file backups, and credentials
  are per-user on all three platforms: a daemon running as a different account,
  or at a different elevation, cannot read them.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys

from .config import env_value

log = logging.getLogger(__name__)

#: Env var enabling the keychain for every login. ``MB_CRAWLER_KEYCHAIN`` is
#: the deprecated pre-rename spelling and still works; the new name wins.
KEYCHAIN_ENV = "MANAGEBAC_KEYCHAIN"
KEYCHAIN_ENV_LEGACY = "MB_CRAWLER_KEYCHAIN"

#: Service name the item is filed under. Kept space-free so the same string
#: stays usable as a Windows credential target, where spaces are delimiters.
SERVICE = "tahuti"

#: Human-readable label shown in Keychain Access / Secret Service prompts.
LABEL = "ManageBac password (tahuti)"

#: Windows exit code meaning "no such credential" — the same ``44``
#: ``secret-tool`` returns for a missing item, so callers need not care which
#: platform produced it.
_PS_NOT_FOUND = 44

_TRUE = {"1", "true", "yes", "on"}

# ── Windows: PowerShell + WinRT PasswordVault ─────────────────────────────
#
# One short-lived ``powershell.exe`` child per operation. The script text lands
# in argv, so it must never contain the secret; the account name and the
# service name do go in argv (neither is confidential) and are quoted as
# PowerShell single-quoted literals, which escape ``'`` by doubling it.

#: Instantiates the vault. Interpolated into each script so the WinRT type
#: string — long and easy to typo — is written down exactly once.
_PS_VAULT = (
    "[Windows.Security.Credentials.PasswordVault,"
    "Windows.Security.Credentials,ContentType=WindowsRuntime]::new()"
)

#: Builds the credential object ``Add``/``Remove`` take. Only ``Retrieve``
#: accepts a ``(resource, user)`` pair; the other two want a
#: ``PasswordCredential``, so the secret is handed over as the constructor's
#: third argument and never parsed as PowerShell source.
_PS_ADD = (
    "[Windows.Security.Credentials.PasswordCredential,"
    "Windows.Security.Credentials,ContentType=WindowsRuntime]"
    "::new({target},{user},$pw)"
)

#: Reads stdin to EOF as raw bytes and decodes UTF-8. Going through
#: ``OpenStandardInput`` rather than ``[Console]::In.ReadToEnd()`` skips the
#: console input encoding entirely, so a password survives byte for byte
#: whatever the machine's code page is.
_PS_READ_STDIN = (
    "$ms=New-Object System.IO.MemoryStream\n"
    "$buf=New-Object byte[] 65536\n"
    "$in=[Console]::OpenStandardInput()\n"
    "while(($n=$in.Read($buf,0,$buf.Length)) -gt 0){$ms.Write($buf,0,$n)}\n"
    "$pw=[Text.Encoding]::UTF8.GetString($ms.ToArray())\n"
)

#: ``{vault}``, ``{target}``, ``{user}`` and ``{cred}`` are substituted by
#: :func:`_ps_script`. ``Remove`` before ``Add`` because a re-login must update
#: in place and ``Add`` is not documented to overwrite an existing pair.
_PS_STORE = (
    "$ErrorActionPreference='Stop'\n"
    + _PS_READ_STDIN
    + "$v={vault}\n"
    + "try{$v.Remove($v.Retrieve({target},{user}))}catch{}\n"
    + "$v.Add({cred})\n"
)

#: ``Retrieve`` fills the object but leaves ``Password`` empty until
#: ``RetrievePassword()`` is called — a classic way to get a blank password
#: back from a vault that is working perfectly. ``Retrieve`` also *throws*
#: rather than returning null when the pair is absent.
_PS_LOOKUP = (
    "$ErrorActionPreference='Stop'\n"
    "$v={vault}\n"
    "try{$c=$v.Retrieve({target},{user})}catch{exit {notfound}}\n"
    "$c.RetrievePassword()\n"
    "if(-not $c.Password){exit {notfound}}\n"
    "[Console]::Out.Write("
    "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($c.Password)))\n"
)

_PS_DELETE = (
    "$ErrorActionPreference='Stop'\n"
    "$v={vault}\n"
    "try{$v.Remove($v.Retrieve({target},{user}))}catch{exit {notfound}}\n"
)


def _ps_quote(value: str) -> str:
    """Quote *value* as a PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _ps_script(template: str, account: str) -> str:
    """Substitute the placeholders in a Windows script.

    *account* originates from the CLI, so it is quoted as a PowerShell
    single-quoted literal rather than spliced in raw — an account name
    containing a quote character must not be able to append statements to the
    script. ``{cred}`` is expanded before ``{target}`` because it contains it,
    and ``{user}`` goes in last so braces in an account name can never be
    mistaken for a placeholder.
    """
    return (
        template.replace("{notfound}", str(_PS_NOT_FOUND))
        .replace("{cred}", _PS_ADD)
        .replace("{vault}", _PS_VAULT)
        .replace("{target}", _ps_quote(SERVICE))
        .replace("{user}", _ps_quote(account))
    )


def _tool() -> list[str] | None:
    """Return the argv prefix for this platform's credential helper, if any.

    On Windows the prefix ends in ``-Command`` and the script is appended per
    operation. Windows PowerShell 5.1 is preferred over PowerShell 7 because
    only .NET Framework projects WinRT types into PowerShell.
    """
    if sys.platform == "darwin":
        exe = shutil.which("security")
        return [exe] if exe else None
    if sys.platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return None
        return [
            exe,
            "-NoProfile",
            "-NoLogo",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        ]
    exe = shutil.which("secret-tool")
    return [exe] if exe else None


def available() -> bool:
    """True when this machine has a supported credential helper on PATH."""
    return _tool() is not None


def enabled(explicit: bool | None = None) -> bool:
    """Whether keychain storage is switched on.

    An explicit ``--keychain`` / ``--no-keychain`` flag wins; otherwise
    ``MANAGEBAC_KEYCHAIN`` (deprecated: ``MB_CRAWLER_KEYCHAIN``) decides, with
    the new name taking precedence when both are set. A machine without a
    usable helper is never "enabled", so callers can rely on this alone to
    pick a backend.
    """
    if explicit is not None:
        return explicit and available()
    value = env_value(KEYCHAIN_ENV, KEYCHAIN_ENV_LEGACY) or ""
    if value.strip().lower() in _TRUE:
        return available()
    return False


def _run(argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=15)


def _store_argv(
    tool: list[str], account: str, secret: str
) -> tuple[list[str], bytes | None]:
    """Return the ``(argv, stdin)`` pair that writes *secret* for *account*.

    macOS is the only backend that cannot take the secret on stdin — see the
    ``security`` note in the module docstring — so there it travels as the
    ``-w`` argument. The other two hand it over as bytes the child never has
    to parse as either source or a command line.
    """
    if sys.platform == "win32":
        return [*tool, _ps_script(_PS_STORE, account)], secret.encode("utf-8")
    if sys.platform == "darwin":
        # -U replaces an existing item instead of erroring on a re-login.
        return [
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
        ], None
    return (
        [*tool, "store", "--label=" + LABEL, SERVICE, account],
        secret.encode("utf-8"),
    )


def _lookup_argv(tool: list[str], account: str) -> list[str]:
    if sys.platform == "win32":
        return [*tool, _ps_script(_PS_LOOKUP, account)]
    if sys.platform == "darwin":
        return [*tool, "find-generic-password", "-s", SERVICE, "-a", account, "-w"]
    return [*tool, "lookup", SERVICE, account]


def _delete_argv(tool: list[str], account: str) -> list[str]:
    if sys.platform == "win32":
        return [*tool, _ps_script(_PS_DELETE, account)]
    if sys.platform == "darwin":
        return [*tool, "delete-generic-password", "-s", SERVICE, "-a", account]
    return [*tool, "clear", SERVICE, account]


def store(account: str, secret: str) -> bool:
    """Store *secret* under *account*. Returns True on success.

    ``True`` means the secret came back **byte for byte**, and it means that on
    every platform — not only on Windows. :mod:`mb_cli.auth` unlinks the
    cleartext ``creds.json`` on a ``True`` return, so a helper that exits 0
    without actually persisting anything would leave the user with no copy of
    the password anywhere. Reading the item straight back turns that into a
    ``False`` and a fallback to ``creds.json``.
    """
    tool = _tool()
    if tool is None or not account or not secret:
        return False
    argv, stdin = _store_argv(tool, account, secret)
    try:
        proc = _run(argv, stdin=stdin)
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
    if lookup(account) != secret:
        # Every backend here is a child process, and any of them can exit 0 for
        # a write that did not land — a locked Secret Service collection on
        # Linux, a D-Bus hiccup, an unwritable keychain on macOS. The read-back
        # is the only thing standing between "reported success" and "the
        # password is gone", so it runs on all three platforms.
        log.warning(
            "OS keychain reported success but the password did not come back "
            "byte for byte — falling back to creds.json"
        )
        return False
    return True


def lookup(account: str) -> str | None:
    """Return the stored secret for *account*, or *None* if absent/unavailable.

    The value is exactly what was stored. ``secret-tool`` and ``security``
    terminate what they print with a newline the secret does not contain, and
    precisely that one newline is removed — stripping *all* trailing newlines
    silently corrupted any password that legitimately ended in one, which then
    never matched in :func:`store` and could not authenticate a silent
    re-login.
    """
    tool = _tool()
    if tool is None or not account:
        return None
    try:
        proc = _run(_lookup_argv(tool, account))
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("OS keychain lookup failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    if sys.platform == "win32":
        # base64 on the way out means nothing PowerShell prints — banners,
        # wrapping, a stray CRLF — can end up inside the password. A decode that
        # fails must not raise: `lookup` is on the daemon's silent-re-login path.
        # binascii.Error and UnicodeDecodeError both subclass ValueError.
        try:
            return (
                base64.b64decode(proc.stdout.strip(), validate=True).decode("utf-8")
                or None
            )
        except ValueError:
            log.debug("OS keychain lookup returned undecodable output")
            return None
    raw = proc.stdout.decode("utf-8", "replace")
    secret = raw[:-1] if raw.endswith("\n") else raw
    return secret or None


def delete(account: str) -> bool:
    """Remove the stored secret for *account*. Returns True if one was removed."""
    tool = _tool()
    if tool is None or not account:
        return False
    try:
        proc = _run(_delete_argv(tool, account))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("OS keychain delete failed: %s", exc)
        return False
    return proc.returncode == 0
