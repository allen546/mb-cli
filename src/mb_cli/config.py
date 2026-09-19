"""Configuration and session persistence for tahuti."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import sys
import tempfile

CONFIG_ENV = "MB_CRAWLER_CONFIG"
SESSION_ENV = "MB_CRAWLER_SESSION"
CREDS_ENV = "MB_CRAWLER_CREDS_PATH"
# Escape hatch so scripts and CI can silence the loose-permission warning.
PERM_WARN_ENV = "MB_CRAWLER_NO_PERM_WARN"

# Permission floor for anything this package writes that can hold a secret.
# Any group- or other-readable bit means every local user can read the file.
SECURE_FILE_MODE = 0o600


@dataclass
class ProfileConfig:
    name: str
    school: str | None = None
    domain: str = "managebac.com"
    email: str | None = None
    default_view: str = "all"
    default_pages: int = 10
    default_subject: str = ""
    default_details: bool = False
    default_format: str = "pretty"
    default_cache_ttl: int = 900


@dataclass
class SessionConfig:
    name: str
    school: str | None = None
    domain: str = "managebac.com"
    email: str | None = None
    base_url: str | None = None
    cookie: str | None = None
    logged_in_at: str | None = None


@dataclass
class AppState:
    config_path: Path
    session_path: Path
    active_profile: str
    profile: ProfileConfig
    session: SessionConfig


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def config_dir() -> Path:
    """Directory holding all persisted state.

    Resolved on every call rather than captured at import. ``Path.home()``
    reads ``$HOME``, so a module-level constant froze whatever the environment
    was when :mod:`mb_cli.config` was first imported — and could then disagree
    with :func:`resolve_creds_path` and its siblings, which re-resolve per call.
    One code path would write to one directory while another read from a
    different one, which is exactly how a saved password ends up invisible to
    the code that goes looking for it.
    """
    return Path.home() / ".config" / "tahuti"


def default_config_path() -> Path:
    return config_dir() / "config.json"


def default_session_path() -> Path:
    return config_dir() / "session.json"


def default_creds_path() -> Path:
    return config_dir() / "creds.json"


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CONFIG_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return default_config_path()


def resolve_session_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(SESSION_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return default_session_path()


def resolve_creds_path(explicit: str | None = None) -> Path:
    """Resolve the file holding the saved password for silent re-login."""
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CREDS_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return default_creds_path()


def clear_creds(path: str | Path) -> bool:
    """Delete the saved password file.

    Returns *True* when a file was actually removed, *False* when there was
    nothing to delete or the unlink failed. Callers surface this so
    ``tahuti logout`` can report honestly rather than claiming a deletion that
    did not happen.
    """
    try:
        Path(path).unlink()
        return True
    except OSError:
        # FileNotFoundError lands here too — "nothing to delete" is not an error.
        return False


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    """Write JSON to *path* with 0600 permissions, atomically.

    The file is created via ``mkstemp`` (0600 from birth) and then
    ``os.replace``d into place, so the plaintext password is never visible
    at a permissive mode, even briefly.
    """
    _ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp_name, SECURE_FILE_MODE)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_state(
    profile_name: str | None = None,
    config_path: str | None = None,
    session_path: str | None = None,
) -> AppState:
    config_file = resolve_config_path(config_path)
    session_file = resolve_session_path(session_path)
    config_data = _read_json(config_file)
    session_data = _read_json(session_file)

    active_profile = (
        profile_name
        or session_data.get("active_profile")
        or config_data.get("active_profile")
        or "default"
    )

    profile_data = config_data.get("profiles", {}).get(active_profile, {})
    defaults = profile_data.get("defaults", {})
    session_profile_data = session_data.get("profiles", {}).get(active_profile, {})

    profile = ProfileConfig(
        name=active_profile,
        school=profile_data.get("school"),
        domain=profile_data.get("domain", "managebac.com"),
        email=profile_data.get("email"),
        default_view=defaults.get("view", "all"),
        default_pages=defaults.get("pages", 10),
        default_subject=defaults.get("subject", ""),
        default_details=defaults.get("details", False),
        default_format=defaults.get("format", "pretty"),
        default_cache_ttl=defaults.get("cache_ttl", 900),
    )
    session = SessionConfig(
        name=active_profile,
        school=session_profile_data.get("school"),
        domain=session_profile_data.get("domain", profile.domain),
        email=session_profile_data.get("email"),
        base_url=session_profile_data.get("base_url"),
        cookie=session_profile_data.get("cookie"),
        logged_in_at=session_profile_data.get("logged_in_at"),
    )
    return AppState(
        config_path=config_file,
        session_path=session_file,
        active_profile=active_profile,
        profile=profile,
        session=session,
    )


def save_profile(state: AppState) -> None:
    config_data = _read_json(state.config_path)
    profiles = config_data.setdefault("profiles", {})
    profiles[state.active_profile] = {
        "school": state.profile.school,
        "domain": state.profile.domain,
        "email": state.profile.email,
        "defaults": {
            "view": state.profile.default_view,
            "pages": state.profile.default_pages,
            "subject": state.profile.default_subject,
            "details": state.profile.default_details,
            "format": state.profile.default_format,
            "cache_ttl": state.profile.default_cache_ttl,
        },
    }
    config_data["version"] = 1
    config_data["active_profile"] = state.active_profile
    _write_json(state.config_path, config_data)


def save_session(state: AppState) -> None:
    session_data = _read_json(state.session_path)
    profiles = session_data.setdefault("profiles", {})
    profiles[state.active_profile] = {
        "school": state.session.school,
        "domain": state.session.domain,
        "email": state.session.email,
        "base_url": state.session.base_url,
        "cookie": state.session.cookie,
        "logged_in_at": state.session.logged_in_at,
    }
    session_data["version"] = 1
    session_data["active_profile"] = state.active_profile
    _write_json(state.session_path, session_data)


def save_creds(path: str | Path, email: str, password: str) -> None:
    """Save email/password to an external JSON file for silent re-login."""
    p = Path(path)
    # Written 0600 from birth via _write_json's atomic temp-file path.
    _write_json(p, {"email": email, "password": password, "version": 1})


def load_creds(path: str | Path) -> dict | None:
    """Load email/password from an external JSON file.

    Returns a dict with ``email`` and/or ``password`` keys, or *None* if the
    file doesn't exist or can't be parsed.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {k: data[k] for k in ("email", "password") if k in data}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_session(state: AppState, all_profiles: bool = False) -> None:
    if all_profiles:
        if state.session_path.exists():
            state.session_path.unlink()
        return

    session_data = _read_json(state.session_path)
    profiles = session_data.get("profiles", {})
    profiles.pop(state.active_profile, None)
    if profiles:
        session_data["profiles"] = profiles
        session_data["version"] = 1
        session_data["active_profile"] = state.active_profile
        _write_json(state.session_path, session_data)
    elif state.session_path.exists():
        state.session_path.unlink()


def file_mode(path: str | Path) -> int | None:
    """Return the file's permission bits, or *None* if it cannot be stat'd."""
    try:
        return Path(path).stat().st_mode & 0o777
    except OSError:
        return None


def is_too_permissive(path: str | Path) -> bool:
    """True when *path* is readable or writable by group/other.

    ``creds.json`` and ``session.json`` are written 0600 by this package, so a
    looser mode means something outside `mb` changed it — a stray backup, a
    `cp` that dropped modes, a config-management tool. Since file permissions
    are the *only* barrier protecting a cleartext password here, silently
    accepting a 0644 creds file would undercut the whole storage model.
    """
    mode = file_mode(path)
    if mode is None:
        return False
    return bool(mode & 0o077)


def insecure_state_files() -> list[Path]:
    """Every existing credential-bearing state file with looser-than-0600 modes."""
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in (
        resolve_creds_path(),
        resolve_session_path(),
        resolve_config_path(),
    ):
        if candidate in seen:
            continue
        seen.add(candidate)
        if is_too_permissive(candidate):
            found.append(candidate)
    return found


def warn_on_weak_permissions(stream=None) -> list[str]:
    """Warn on stderr about any credential file readable by other local users.

    Returns the warnings emitted. Writes to *stream* (stderr by default) rather
    than using :mod:`logging` so the message survives a caller that has
    reconfigured logging, and never contaminates ``--format json`` stdout.
    """
    stream = sys.stderr if stream is None else stream
    insecure = insecure_state_files()
    if not insecure:
        return []
    messages = [
        f"{path} is mode {file_mode(path):04o} — readable by other users on this "
        f"machine. Your ManageBac password or session cookie may be exposed; run "
        f"`chmod 600 {path}`."
        for path in insecure
    ]
    if not os.environ.get(PERM_WARN_ENV):
        for message in messages:
            print(f"warning: {message}", file=stream)
    return messages


#: The pre-lazy names, kept importable so out-of-tree callers do not break.
#: Each resolves on attribute access, so unlike the module-level constants they
#: replaced they cannot go stale when ``$HOME`` changes after import.
_LEGACY_PATHS = {
    "CONFIG_DIR": config_dir,
    "DEFAULT_CONFIG_PATH": default_config_path,
    "DEFAULT_SESSION_PATH": default_session_path,
    "DEFAULT_CREDS_PATH": default_creds_path,
}


def __getattr__(name: str):
    """Resolve ``CONFIG_DIR`` / ``DEFAULT_*_PATH`` on access, not at import."""
    resolver = _LEGACY_PATHS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()
