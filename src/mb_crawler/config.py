"""Configuration and session persistence for mb-crawler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os

CONFIG_ENV = "MB_CRAWLER_CONFIG"
SESSION_ENV = "MB_CRAWLER_SESSION"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mb-crawler" / "config.json"
DEFAULT_SESSION_PATH = Path.home() / ".config" / "mb-crawler" / "session.json"


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
    default_cache_ttl: int = 1800


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


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CONFIG_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_CONFIG_PATH


def resolve_session_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(SESSION_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_SESSION_PATH


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    _ensure_parent(path)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.chmod(path, 0o600)


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
        default_cache_ttl=defaults.get("cache_ttl", 1800),
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
