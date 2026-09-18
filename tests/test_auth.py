"""Tests for mb_cli.auth."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli import auth
from mb_cli.auth import build_client, hub_client, session_email
from mb_cli.client import ManageBacClient
from mb_cli.config import AppState, ProfileConfig, SessionConfig, load_creds
from mb_cli.exceptions import CommandError


# ── which email identifies this profile's on-disk state ───────────────────
#
# The response-cache directory is a hash of the email and the keychain item is
# filed under it, so `build_client` and `logout` must pick the same one. They
# used to disagree: build_client took `--email`, then the profile's, then the
# session's; logout took the session's first. With profile and session emails
# differing, logout deleted another profile's hash directory and left the
# JWT-bearing entries in place while reporting success.


def _state(profile_email=None, session_email_=None):
    return AppState(
        config_path=Path("config.json"),
        session_path=Path("session.json"),
        active_profile="default",
        profile=ProfileConfig(name="default", email=profile_email),
        session=SessionConfig(name="default", email=session_email_),
    )


class TestSessionEmail:
    def test_explicit_override_wins(self):
        state = _state(profile_email="profile@example.com", session_email_="session@example.com")
        assert session_email(state, "flag@example.com") == "flag@example.com"

    def test_profile_email_beats_session_email(self):
        """The precedence logout got backwards."""
        state = _state(profile_email="profile@example.com", session_email_="session@example.com")
        assert session_email(state) == "profile@example.com"

    def test_session_email_is_the_fallback(self):
        state = _state(session_email_="session@example.com")
        assert session_email(state) == "session@example.com"

    def test_neither_set_is_empty(self):
        assert session_email(_state()) == ""

    @pytest.mark.parametrize("blank", ["", None])
    def test_a_blank_value_does_not_shadow_a_real_one(self, blank):
        """`or` semantics: an empty string must fall through, not win."""
        state = _state(profile_email=blank, session_email_="session@example.com")
        assert session_email(state) == "session@example.com"
        assert session_email(state, blank) == "session@example.com"

    def test_build_client_keys_its_cache_dir_by_the_same_email(self, tmp_path, monkeypatch):
        """Proves the helper is what build_client actually uses.

        `logout` has no `--email` of its own, so once it calls this helper the
        cache directory it clears is the one build_client populated.
        """
        # Isolate the state dirs: without this build_client falls through to
        # `_load_creds()`, which resolves to the developer's real creds.json.
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))
        monkeypatch.setenv("MB_CRAWLER_CREDS_PATH", str(tmp_path / "creds.json"))

        profile_email = "profile@example.com"
        state = _state(profile_email=profile_email, session_email_="session@example.com")
        with (
            patch("mb_cli.auth.load_state", return_value=state),
            patch("mb_cli.auth.ManageBacClient") as client_cls,
            patch("mb_cli.auth._store_password") as store_password,
        ):
            client_cls.return_value.login.return_value = True
            _, _, email = build_client(
                school="bj80", email=profile_email, password="pw", remember=False
            )

        assert email == profile_email
        assert store_password.assert_not_called() is None
        import hashlib

        expected = (
            Path.home()
            / ".config"
            / "tahuti"
            / "cache"
            / hashlib.sha256(profile_email.encode()).hexdigest()[:16]
        )
        assert client_cls.call_args.kwargs["cache"].cache_dir == expected


# ── the hub must follow the client's TLS decision ─────────────────────────
#
# `MNNHubClient` defaults to `verify=True`, and four call sites built it
# directly — client.py:1820, client.py:1931, __main__.py:1116 and
# mcp_server.py:560/605 — so `--no-verify-tls` applied to ManageBac and not to
# the hub. `hub_client()` is the one construction point; `verify` is
# keyword-only with no default so the omission cannot come back silently.


class TestHubClientHonoursTLS:
    def test_verify_is_required(self):
        """Omitting it must raise, not quietly mean `verify=True`."""
        with pytest.raises(TypeError):
            auth.hub_client("https://mnn-hub.example", "token")

    @pytest.mark.parametrize(
        "verify", [True, False, "/etc/ssl/certs/internal-ca.pem", ""]
    )
    def test_verify_reaches_the_session(self, verify):
        """Whatever the caller decided, the hub uses it — including a bundle."""
        hub = auth.hub_client("https://mnn-hub.example", "tok", verify=verify)
        assert hub.session.verify == verify

    def test_follows_the_managebac_clients_decision(self):
        """The intended call: pass `client.session.verify` straight through."""
        client = ManageBacClient("bj80", verify=False)
        hub = auth.hub_client(
            "https://mnn-hub.example", "tok", verify=client.session.verify
        )
        assert hub.session.verify is False
        assert hub.session.verify == client.session.verify

    def test_follows_a_ca_bundle_the_client_was_given(self):
        bundle = "/etc/ssl/certs/internal-ca.pem"
        client = ManageBacClient("bj80", verify=bundle)
        hub = auth.hub_client(
            "https://mnn-hub.example", "tok", verify=client.session.verify
        )
        assert hub.session.verify == bundle

    def test_endpoint_and_token_are_still_forwarded(self):
        hub = auth.hub_client("https://mnn-hub.example", "the-token", verify=True)
        assert hub.base == "https://mnn-hub.example/api/frontend/v2"
        assert hub.session.headers["Authorization"] == "Bearer the-token"


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
            str(Path.home() / ".config" / "tahuti" / "creds.json")
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
