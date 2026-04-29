"""Tests for mb_crawler.config."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mb_crawler.config import (
    AppState,
    ProfileConfig,
    SessionConfig,
    clear_session,
    load_state,
    resolve_config_path,
    resolve_session_path,
    save_profile,
    save_session,
)


class TestResolveConfigPath:
    def test_explicit_path(self):
        assert resolve_config_path("/my/path") == Path("/my/path")

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", "/env/config.json")
        assert resolve_config_path(None) == Path("/env/config.json")

    def test_default(self, monkeypatch):
        monkeypatch.delenv("MB_CRAWLER_CONFIG", raising=False)
        result = resolve_config_path(None)
        assert result.name == "config.json"
        assert "mb-crawler" in str(result)


class TestResolveSessionPath:
    def test_explicit_path(self):
        assert resolve_session_path("/my/session") == Path("/my/session")

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_SESSION", "/env/session.json")
        assert resolve_session_path(None) == Path("/env/session.json")

    def test_default(self, monkeypatch):
        monkeypatch.delenv("MB_CRAWLER_SESSION", raising=False)
        result = resolve_session_path(None)
        assert result.name == "session.json"
        assert "mb-crawler" in str(result)


class TestLoadState:
    def test_default_when_no_files(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))
        state = load_state()
        assert state.active_profile == "default"
        assert state.profile.domain == "managebac.com"
        assert state.session.cookie is None

    def test_loads_from_existing_files(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(json.dumps({
            "active_profile": "test",
            "profiles": {
                "test": {
                    "school": "bj80",
                    "domain": "managebac.cn",
                    "email": "test@example.com",
                    "defaults": {
                        "view": "upcoming",
                        "pages": 5,
                        "subject": "Math",
                        "details": True,
                        "format": "json",
                        "cache_ttl": 600,
                    },
                }
            }
        }))
        session_path.write_text(json.dumps({
            "active_profile": "test",
            "profiles": {
                "test": {
                    "school": "bj80",
                    "domain": "managebac.cn",
                    "email": "test@example.com",
                    "base_url": "https://bj80.managebac.cn",
                    "cookie": "session_cookie_123",
                    "logged_in_at": "2026-04-29T12:00:00",
                }
            }
        }))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        assert state.active_profile == "test"
        assert state.profile.school == "bj80"
        assert state.profile.email == "test@example.com"
        assert state.profile.default_view == "upcoming"
        assert state.profile.default_pages == 5
        assert state.profile.default_subject == "Math"
        assert state.profile.default_details is True
        assert state.profile.default_cache_ttl == 600
        assert state.session.cookie == "session_cookie_123"
        assert state.session.base_url == "https://bj80.managebac.cn"

    def test_profile_name_override(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(json.dumps({
            "profiles": {"alpha": {"school": "s1"}, "beta": {"school": "s2"}}
        }))
        session_path.write_text(json.dumps({
            "profiles": {"alpha": {"cookie": "c1"}, "beta": {"cookie": "c2"}}
        }))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state(profile_name="beta")
        assert state.active_profile == "beta"
        assert state.profile.school == "s2"
        assert state.session.cookie == "c2"


class TestSaveProfile:
    def test_creates_and_writes_profile(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        state.profile.school = "bj80"
        state.profile.email = "me@example.com"
        save_profile(state)

        data = json.loads(config_path.read_text())
        assert data["profiles"]["default"]["school"] == "bj80"
        assert data["profiles"]["default"]["email"] == "me@example.com"
        assert data["version"] == 1

    def test_preserves_other_profiles(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(json.dumps({
            "profiles": {
                "existing": {"school": "old_school"},
                "default": {},
            }
        }))
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        state.profile.school = "new_school"
        save_profile(state)

        data = json.loads(config_path.read_text())
        assert data["profiles"]["existing"]["school"] == "old_school"
        assert data["profiles"]["default"]["school"] == "new_school"


class TestSaveSession:
    def test_creates_and_writes_session(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        state.session.cookie = "my_cookie"
        state.session.base_url = "https://bj80.managebac.cn"
        save_session(state)

        data = json.loads(session_path.read_text())
        assert data["profiles"]["default"]["cookie"] == "my_cookie"
        assert data["profiles"]["default"]["base_url"] == "https://bj80.managebac.cn"


class TestClearSession:
    def test_clear_single_profile(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(json.dumps({
            "profiles": {
                "default": {"cookie": "c1"},
                "other": {"cookie": "c2"},
            }
        }))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        clear_session(state)

        data = json.loads(session_path.read_text())
        assert "default" not in data["profiles"]
        assert data["profiles"]["other"]["cookie"] == "c2"

    def test_clear_all_profiles(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(json.dumps({
            "profiles": {"default": {"cookie": "c1"}, "other": {"cookie": "c2"}}
        }))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        clear_session(state, all_profiles=True)
        assert not session_path.exists()

    def test_clear_last_profile_removes_file(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        session_path.write_text(json.dumps({
            "profiles": {"default": {"cookie": "c1"}}
        }))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state = load_state()
        clear_session(state)
        assert not session_path.exists()
