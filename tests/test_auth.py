"""Tests for mb_cli.auth."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.auth import build_client
from mb_cli.config import load_creds
from mb_cli.exceptions import CommandError


def test_load_creds_reads_email_and_password():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"email": "test@example.com", "password": "secret123"}, f)
        path = f.name
    try:
        creds = load_creds(path)
        assert creds == {"email": "test@example.com", "password": "secret123"}
    finally:
        os.unlink(path)


def test_load_creds_missing_file_returns_none():
    creds = load_creds("/nonexistent/path.json")
    assert creds is None


def test_load_creds_missing_keys_returns_partial():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"email": "test@example.com"}, f)
        path = f.name
    try:
        creds = load_creds(path)
        assert creds == {"email": "test@example.com"}
    finally:
        os.unlink(path)


class TestBuildClient:
    def test_missing_school_raises_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))
        with pytest.raises(CommandError) as exc_info:
            build_client()
        assert exc_info.value.code == "missing_credentials"

    def test_missing_email_raises_error(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        with patch("mb_cli.auth.load_creds", return_value=None):
            with pytest.raises(CommandError) as exc_info:
                build_client(school="bj80", password=None)
        assert exc_info.value.code == "missing_credentials"

    def test_missing_password_raises_error(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "default": {"school": "bj80", "email": "test@example.com"}
                    }
                }
            )
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        with patch("mb_cli.auth.load_creds", return_value=None):
            with pytest.raises(CommandError) as exc_info:
                build_client(school="bj80", email="test@example.com", password=None)
        assert exc_info.value.code == "missing_credentials"

    def test_cookie_auth(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {"profiles": {"default": {"school": "bj80", "domain": "managebac.cn"}}}
            )
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(
            school="bj80",
            domain="managebac.cn",
            cookie="my_cookie_value",
        )
        assert client.school == "bj80"
        assert client.domain == "managebac.cn"
        assert client.session.cookies.get("_managebac_session") == "my_cookie_value"
        assert state.profile.school == "bj80"

    @patch("mb_cli.auth.ManageBacClient")
    def test_password_auth(self, MockClient, tmp_path: Path, monkeypatch):
        mock_instance = MockClient.return_value
        mock_instance.login.return_value = True

        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(
            school="bj80",
            email="test@example.com",
            password="secret",
        )
        mock_instance.login.assert_called_once_with(
            "test@example.com", "secret", remember=True
        )
        assert email == "test@example.com"

    @patch("mb_cli.auth.ManageBacClient")
    def test_cache_directory_namespacing(self, MockClient, tmp_path: Path, monkeypatch):
        mock_instance = MockClient.return_value
        mock_instance.login.return_value = True

        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        with patch("mb_cli.auth.ResponseCache") as MockCache:
            build_client(
                school="bj80",
                email="user@example.com",
                password="secret",
            )
            import hashlib
            expected_hash = hashlib.sha256(b"user@example.com").hexdigest()[:16]
            call_args = MockCache.call_args[1]
            assert call_args["cache_dir"].name == expected_hash

    @patch("mb_cli.auth.ManageBacClient")
    def test_password_auth_failure(self, MockClient, tmp_path: Path, monkeypatch):
        mock_instance = MockClient.return_value
        mock_instance.login.return_value = False

        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        with pytest.raises(CommandError) as exc_info:
            build_client(
                school="bj80",
                email="test@example.com",
                password="wrong",
            )
        assert exc_info.value.code == "authentication_failed"

    @patch("mb_cli.auth._is_session_alive", return_value=True)
    def test_session_cookie_reuse(self, mock_alive, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(json.dumps({}))
        session_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "default": {
                            "cookie": "saved_cookie",
                            "school": "bj80",
                            "domain": "managebac.cn",
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client()
        assert client.session.cookies.get("_managebac_session") == "saved_cookie"

    def test_reauth_skips_saved_cookie(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {"profiles": {"default": {"school": "bj80", "email": "a@b.com"}}}
            )
        )
        session_path.write_text(
            json.dumps(
                {"profiles": {"default": {"cookie": "old_cookie", "school": "bj80"}}}
            )
        )
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        with patch("mb_cli.auth.load_creds", return_value=None):
            with pytest.raises(CommandError) as exc_info:
                build_client(reauth=True, password=None)
        assert exc_info.value.code == "missing_credentials"

    def test_domain_from_config(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {"profiles": {"default": {"school": "bj80", "domain": "managebac.cn"}}}
            )
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(cookie="c")
        assert client.domain == "managebac.cn"

    def test_refresh_disables_cache(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(cookie="c", refresh=True)
        assert client.cache.enabled is False

    def test_cache_ttl_override(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "default": {"school": "bj80", "defaults": {"cache_ttl": 100}}
                    }
                }
            )
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(cookie="c")
        assert client.cache.ttl == 100

    def test_verify_false(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(cookie="c", verify=False)
        assert client.session.verify is False

    def test_retry_config(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        session_path = tmp_path / "session.json"
        config_path.write_text(
            json.dumps({"profiles": {"default": {"school": "bj80"}}})
        )
        session_path.write_text(json.dumps({}))
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(config_path))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(session_path))

        state, client, email = build_client(cookie="c", retry=5)
        assert client.retry == 5

    @patch("mb_cli.auth.save_session")
    @patch("mb_cli.auth.ManageBacClient")
    @patch("mb_cli.auth.load_creds")
    @patch("mb_cli.auth.load_state")
    def test_relogin_on_expired_cookie(self, mock_load_state, mock_load_creds, MockClient, mock_save_session):
        """When saved cookie fails health check, re-login with creds from mb_config.json."""
        mock_state = MagicMock()
        mock_state.profile.school = "bj80"
        mock_state.profile.domain = "managebac.cn"
        mock_state.profile.email = "allen@example.com"
        mock_state.profile.default_cache_ttl = 1800
        mock_state.session.cookie = "dead_cookie"
        mock_state.session.school = "bj80"
        mock_state.session.domain = "managebac.cn"
        mock_state.session.email = "allen@example.com"
        mock_load_state.return_value = mock_state
        mock_load_creds.return_value = {"email": "allen@example.com", "password": "pass123"}

        mock_client = MockClient.return_value
        # Mock redirects to /login on the new health check endpoint (/student/dashboard)
        mock_response = MagicMock(status_code=302)
        mock_response.headers = {"Location": "https://bj80.managebac.cn/login"}
        mock_client.session.get.return_value = mock_response
        mock_client.login.return_value = True
        mock_client.session.cookies.get.return_value = "NEW_COOKIE"

        state, client, email = build_client(reauth=False)

        from pathlib import Path
        mock_load_creds.assert_called_once_with(
            str(Path.home() / ".config" / "mb-crawler" / "creds.json")
        )
        mock_client.login.assert_called_once_with(
            "allen@example.com", "pass123", remember=True
        )
        mock_save_session.assert_called_once()

    @patch("mb_cli.auth.save_session")
    @patch("mb_cli.auth.ManageBacClient")
    @patch("mb_cli.auth.load_creds")
    @patch("mb_cli.auth.load_state")
    def test_relogin_on_401(self, mock_load_state, mock_load_creds, MockClient, mock_save_session):
        """When health check returns 401, re-login with creds."""
        mock_state = MagicMock()
        mock_state.profile.school = "bj80"
        mock_state.profile.domain = "managebac.cn"
        mock_state.profile.email = "allen@example.com"
        mock_state.profile.default_cache_ttl = 1800
        mock_state.session.cookie = "dead_cookie"
        mock_state.session.school = "bj80"
        mock_state.session.domain = "managebac.cn"
        mock_state.session.email = "allen@example.com"
        mock_load_state.return_value = mock_state
        mock_load_creds.return_value = {"email": "allen@example.com", "password": "pass123"}

        mock_client = MockClient.return_value
        mock_client.session.get.return_value = MagicMock(status_code=401)
        mock_client.login.return_value = True
        mock_client.session.cookies.get.return_value = "NEW_COOKIE"

        state, client, email = build_client(reauth=False)

        mock_client.login.assert_called_once_with(
            "allen@example.com", "pass123", remember=True
        )
        mock_save_session.assert_called_once()

    @patch("mb_cli.auth.save_session")
    @patch("mb_cli.auth.ManageBacClient")
    @patch("mb_cli.auth.load_creds")
    @patch("mb_cli.auth.load_state")
    def test_relogin_saves_session(self, mock_load_state, mock_load_creds, MockClient, mock_save_session):
        """After successful re-login, new cookie is persisted to session file."""
        mock_state = MagicMock()
        mock_state.profile.school = "bj80"
        mock_state.profile.domain = "managebac.cn"
        mock_state.profile.email = "allen@example.com"
        mock_state.profile.default_cache_ttl = 1800
        mock_state.session.cookie = "dead_cookie"
        mock_state.session.school = "bj80"
        mock_state.session.domain = "managebac.cn"
        mock_state.session.email = "allen@example.com"
        mock_load_state.return_value = mock_state
        mock_load_creds.return_value = {"email": "allen@example.com", "password": "pass123"}

        mock_client = MockClient.return_value
        mock_client.session.get.return_value = MagicMock(status_code=401)
        mock_client.login.return_value = True
        # Simulate the cookie being set after login
        mock_client.session.cookies.get.return_value = "NEW_COOKIE_VALUE"

        state, client, email = build_client(reauth=False)

        # Verify session was saved with new cookie
        mock_save_session.assert_called_once()
        saved_state = mock_save_session.call_args[0][0]
        assert saved_state.session.cookie == "NEW_COOKIE_VALUE"

    @patch("mb_cli.auth.save_session")
    @patch("mb_cli.auth.ManageBacClient")
    @patch("mb_cli.auth.load_creds")
    @patch("mb_cli.auth.load_state")
    def test_relogin_failure_raises_error(self, mock_load_state, mock_load_creds, MockClient, mock_save_session):
        """Silent re-login raises CommandError when client.login() returns False."""
        mock_state = MagicMock()
        mock_state.profile.school = "bj80"
        mock_state.profile.domain = "managebac.cn"
        mock_state.profile.email = "allen@example.com"
        mock_state.profile.default_cache_ttl = 1800
        mock_state.session.cookie = "dead_cookie"
        mock_state.session.school = "bj80"
        mock_state.session.domain = "managebac.cn"
        mock_state.session.email = "allen@example.com"
        mock_load_state.return_value = mock_state
        mock_load_creds.return_value = {"email": "allen@example.com", "password": "pass123"}

        mock_client = MockClient.return_value
        mock_response = MagicMock(status_code=302)
        mock_response.headers = {"Location": "https://bj80.managebac.cn/login"}
        mock_client.session.get.return_value = mock_response
        mock_client.login.return_value = False

        with pytest.raises(CommandError) as exc_info:
            build_client(reauth=False)
        assert exc_info.value.code == "authentication_failed"

    @patch("mb_cli.auth.save_session")
    @patch("mb_cli.auth.ManageBacClient")
    @patch("mb_cli.auth.load_creds", return_value=None)
    @patch("mb_cli.auth.load_state")
    def test_relogin_missing_creds_file_raises_error(self, mock_load_state, mock_load_creds, MockClient, mock_save_session):
        """Silent re-login raises CommandError when creds file is missing or incomplete."""
        mock_state = MagicMock()
        mock_state.profile.school = "bj80"
        mock_state.profile.domain = "managebac.cn"
        mock_state.profile.email = "allen@example.com"
        mock_state.profile.default_cache_ttl = 1800
        mock_state.session.cookie = "dead_cookie"
        mock_state.session.school = "bj80"
        mock_state.session.domain = "managebac.cn"
        mock_state.session.email = "allen@example.com"
        mock_load_state.return_value = mock_state

        mock_client = MockClient.return_value
        mock_response = MagicMock(status_code=302)
        mock_response.headers = {"Location": "https://bj80.managebac.cn/login"}
        mock_client.session.get.return_value = mock_response

        with pytest.raises(CommandError) as exc_info:
            build_client(reauth=False)
        assert exc_info.value.code == "missing_credentials"
