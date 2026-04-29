"""Tests for mb_crawler.auth."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_crawler.auth import build_client
from mb_crawler.exceptions import CommandError


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

    @patch("mb_crawler.auth.ManageBacClient")
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
        mock_instance.login.assert_called_once_with("test@example.com", "secret")
        assert email == "test@example.com"

    @patch("mb_crawler.auth.ManageBacClient")
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

    def test_session_cookie_reuse(self, tmp_path: Path, monkeypatch):
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
