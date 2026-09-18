"""Tests for the credential-handling overhaul.

Covers four behaviours that were previously either wrong or undocumented:

1. ``mb logout`` now deletes the stored password (``creds.json``) by default.
2. ``mb login --temp`` writes nothing to disk — no password, no session, and
   (new) no response cache.
3. Loose file permissions on credential-bearing state files are reported.
4. ``MB_CRAWLER_PASSWORD`` / ``MB_CRAWLER_COOKIE`` are readable, not just
   exported, plus the optional dependency-free OS keychain backend.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli import __main__ as m
from mb_cli import keychain
from mb_cli.auth import _load_creds, _store_password, build_client
from mb_cli.config import (
    clear_creds,
    insecure_state_files,
    is_too_permissive,
    resolve_creds_path,
    warn_on_weak_permissions,
)

# Env vars every test here must control, so a developer's real ~/.config/tahuti
# (and any leaked MB_CRAWLER_* from the shell) cannot influence the result.
_ISOLATE = (
    "MB_CRAWLER_CONFIG",
    "MB_CRAWLER_SESSION",
    "MB_CRAWLER_CREDS_PATH",
    "MB_CRAWLER_PASSWORD",
    "MB_CRAWLER_COOKIE",
    "MB_CRAWLER_KEYCHAIN",
    "MB_CRAWLER_NO_PERM_WARN",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every state path at tmp_path and clear all credential env vars."""
    for var in _ISOLATE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))
    monkeypatch.setenv("MB_CRAWLER_CREDS_PATH", str(tmp_path / "creds.json"))
    # Pretend a credential helper exists so backend-selection is deterministic
    # on any machine. Tests that need "no helper" patch _tool to None instead.
    monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/fake-helper"])
    yield tmp_path


def _write_creds(path: Path, email="student@example.com", password="s3cret") -> None:
    path.write_text(json.dumps({"email": email, "password": password, "version": 1}))


def _write_session(path: Path, email="student@example.com", cookie="cookieval") -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_profile": "default",
                "profiles": {
                    "default": {
                        "school": "bj80",
                        "domain": "managebac.com",
                        "email": email,
                        "base_url": "https://bj80.managebac.com",
                        "cookie": cookie,
                        "logged_in_at": "2026-09-18T00:00:00",
                    }
                },
            }
        )
    )


def _logout_argv(*extra):
    return ["logout", "--format", "json", *extra]


# ── Task 2.1 — logout deletes the stored password ────────────────────────


class TestLogoutDeletesCredentials:
    def test_logout_removes_creds_json(self, isolated_env, capsys):
        """The whole point: `logout` must not leave the password on disk."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        assert creds.exists()

        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))

        assert not creds.exists(), "logout left creds.json (the password) on disk"

    def test_logout_reports_removal(self, isolated_env, capsys):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))
        payload = json.loads(capsys.readouterr().out)
        data = payload["data"]
        assert data["credentials_removed"] is True
        assert data["credentials_kept"] is False

    def test_keep_credentials_preserves_password(self, isolated_env):
        """`--keep-credentials` is the documented opt-out for silent re-login."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        assert creds.exists(), "--keep-credentials should keep creds.json"
        assert json.loads(creds.read_text())["password"] == "s3cret"

    def test_keep_credentials_reports_kept(self, isolated_env, capsys):
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_kept"] is True
        assert payload["data"]["credentials_removed"] is False

    def test_logout_without_creds_is_not_an_error(self, isolated_env, capsys):
        """Nothing to delete must not raise or claim a false removal."""
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            assert m.cmd_logout(m.build_parser().parse_args(_logout_argv())) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["credentials_removed"] is False

    def test_logout_deletes_keychain_entry(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        session = Path(os.environ["MB_CRAWLER_SESSION"])
        _write_session(session)
        with (
            patch("mb_cli.cache.ResponseCache") as cache_cls,
            patch.object(keychain, "delete", return_value=True) as delete,
        ):
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv()))
        delete.assert_called_once_with("student@example.com")

    def test_keep_credentials_leaves_keychain_entry(self, isolated_env):
        session = Path(os.environ["MB_CRAWLER_SESSION"])
        _write_session(session)
        with (
            patch("mb_cli.cache.ResponseCache") as cache_cls,
            patch.object(keychain, "delete", return_value=True) as delete,
        ):
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(
                m.build_parser().parse_args(_logout_argv("--keep-credentials"))
            )
        delete.assert_not_called()

    def test_logout_all_profiles_removes_creds(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            m.cmd_logout(m.build_parser().parse_args(_logout_argv("--all")))
        assert not creds.exists()


class TestClearCreds:
    def test_removes_file_and_reports_true(self, tmp_path):
        p = tmp_path / "creds.json"
        _write_creds(p)
        assert clear_creds(p) is True
        assert not p.exists()

    def test_missing_file_returns_false(self, tmp_path):
        assert clear_creds(tmp_path / "nope.json") is False


# ── Task 2.2 — `--temp` writes nothing to disk ───────────────────────────


class TestTempModeWritesNothing:
    """`mb login --temp` must leave no reusable credential behind.

    `remember=False` already skipped saving the password; the response cache
    and the session file were still written, which quietly defeated the flag.
    """

    def _build(self, **kwargs):
        client = MagicMock()
        client.login.return_value = True
        client.session.cookies.get.return_value = "newcookie"
        with patch("mb_cli.auth.ManageBacClient", return_value=client):
            return build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
                **kwargs,
            )

    def test_temp_does_not_write_creds_json(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        self._build(remember=False)
        assert not creds.exists(), "--temp persisted the password"

    def test_temp_sends_remember_me_zero(self, isolated_env):
        client = MagicMock()
        client.login.return_value = True
        with patch("mb_cli.auth.ManageBacClient", return_value=client):
            build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
                remember=False,
            )
        assert client.login.call_args.kwargs["remember"] is False

    def test_temp_disables_response_cache(self, isolated_env):
        """The cache holds grade pages and the MNN-hub JWT — not `--temp`-safe."""
        with patch("mb_cli.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
                remember=False,
            )
        cache = client_cls.call_args.kwargs["cache"]
        assert cache.enabled is False

    def test_default_login_enables_cache(self, isolated_env):
        with patch("mb_cli.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
            )
        cache = client_cls.call_args.kwargs["cache"]
        assert cache.enabled is True

    def test_temp_does_not_write_session(self, isolated_env, tmp_path):
        session = tmp_path / "session.json"
        # No saved session and no cookie: the creds-file branch runs, which used
        # to persist a reusable cookie even under --temp.
        _write_creds(tmp_path / "creds.json")
        with patch("mb_cli.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            build_client(school="bj80", remember=False)
        assert not session.exists(), "--temp persisted a reusable session cookie"

    def test_temp_writes_nothing_to_config_dir(self, isolated_env, tmp_path):
        """Belt-and-braces: nothing at all lands beside the config file."""
        before = {p.name for p in tmp_path.iterdir()}
        self._build(remember=False)
        after = {p.name for p in tmp_path.iterdir()}
        assert after - before <= set(), f"--temp wrote new files: {after - before}"

    def test_default_login_still_saves_password(self, isolated_env):
        """Control: the non-temp path must keep silent re-login working."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        self._build()
        assert creds.exists()
        assert json.loads(creds.read_text())["password"] == "s3cret"


# ── Task 2.3 — warn on weak file permissions ─────────────────────────────


class TestWeakPermissionWarning:
    def _chmod(self, path: Path, mode: int) -> None:
        path.write_text("{}")
        os.chmod(path, mode)

    @pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o777, 0o604])
    def test_flags_looser_than_0600(self, tmp_path, mode):
        p = tmp_path / "creds.json"
        self._chmod(p, mode)
        assert is_too_permissive(p) is True

    @pytest.mark.parametrize("mode", [0o600, 0o400, 0o000])
    def test_accepts_0600_or_tighter(self, tmp_path, mode):
        p = tmp_path / "creds.json"
        self._chmod(p, mode)
        assert is_too_permissive(p) is False

    def test_missing_file_is_not_permissive(self, tmp_path):
        assert is_too_permissive(tmp_path / "absent.json") is False

    def test_warns_about_world_readable_creds(self, isolated_env, tmp_path):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert len(messages) == 1
        assert "creds.json" in messages[0]
        assert "0644" in messages[0]
        assert "chmod 600" in messages[0]

    def test_warns_about_config_json(self, isolated_env, tmp_path):
        config = tmp_path / "config.json"
        config.write_text("{}")
        os.chmod(config, 0o644)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert any("config.json" in msg for msg in messages)

    def test_warns_about_session_json(self, isolated_env, tmp_path):
        """session.json holds a live cookie, so it is checked too."""
        session = tmp_path / "session.json"
        _write_session(session)
        os.chmod(session, 0o666)
        messages = warn_on_weak_permissions(stream=open(os.devnull, "w"))
        assert any("session.json" in msg for msg in messages)

    def test_no_warning_when_all_0600(self, isolated_env, tmp_path):
        for name in ("config.json", "session.json", "creds.json"):
            p = tmp_path / name
            p.write_text("{}")
            os.chmod(p, 0o600)
        assert warn_on_weak_permissions(stream=open(os.devnull, "w")) == []

    def test_warning_goes_to_stderr(self, isolated_env, capsys):
        """Must not contaminate `--format json` on stdout."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        warn_on_weak_permissions()
        captured = capsys.readouterr()
        assert "creds.json" in captured.err
        assert captured.out == ""

    def test_env_var_suppresses_output_but_not_detection(self, isolated_env, monkeypatch, capsys):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        monkeypatch.setenv("MB_CRAWLER_NO_PERM_WARN", "1")
        messages = warn_on_weak_permissions()
        assert messages, "detection should still happen for programmatic callers"
        assert capsys.readouterr().err == ""

    def test_insecure_state_files_lists_every_loose_file(self, isolated_env, tmp_path):
        loose = tmp_path / "creds.json"
        _write_creds(loose)
        os.chmod(loose, 0o644)
        tight = tmp_path / "session.json"
        tight.write_text("{}")
        os.chmod(tight, 0o600)
        found = {p.name for p in insecure_state_files()}
        assert "creds.json" in found
        assert "session.json" not in found

    def test_main_warns_on_startup(self, isolated_env, capsys):
        """The warning has to actually reach a user running a normal command."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)
        os.chmod(creds, 0o644)
        session = Path(os.environ["MB_CRAWLER_SESSION"])
        _write_session(session)
        # `logout` needs no network, so it exercises main() end to end.
        with patch("mb_cli.cache.ResponseCache") as cache_cls:
            cache_cls.return_value.clear.return_value = 0
            with pytest.raises(SystemExit) as exc:
                m.main(["logout", "--format", "json"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "creds.json" in captured.err
        # stdout stays valid JSON despite the stderr warning
        assert json.loads(captured.out)["data"]["logged_out"] is True


# ── Task 2.4 — optional, dependency-free OS keychain ─────────────────────


class TestKeychainModule:
    def test_no_helper_means_unavailable(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.available() is False

    def test_disabled_by_default(self, isolated_env):
        assert keychain.enabled() is False

    def test_env_var_enables(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_env_var_truthy_values(self, isolated_env, monkeypatch, value):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", value)
        assert keychain.enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_env_var_falsy_values(self, isolated_env, monkeypatch, value):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", value)
        assert keychain.enabled() is False

    def test_explicit_flag_overrides_env(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "0")
        assert keychain.enabled(True) is True
        monkeypatch.setenv("MB_CRAWLER_KEYCHAIN", "1")
        assert keychain.enabled(False) is False

    def test_explicit_flag_cannot_enable_without_helper(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.enabled(True) is False

    def test_argv_shape_on_linux(self, monkeypatch):
        """secret-tool takes the secret on stdin, never argv."""
        monkeypatch.setattr(keychain.sys, "platform", "linux")
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/secret-tool"])
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["stdin"] = kwargs.get("stdin")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is True
        assert seen["argv"][0].endswith("secret-tool")
        assert "store" in seen["argv"]
        assert "pw" not in seen["argv"], "secret leaked into argv"
        assert seen["stdin"] == b"pw"
    def test_argv_shape_on_macos(self, monkeypatch):
        monkeypatch.setattr(keychain.sys, "platform", "darwin")
        monkeypatch.setattr(keychain, "_tool", lambda: ["/usr/bin/security"])
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is True
        assert seen["argv"][0].endswith("security")
        assert "add-generic-password" in seen["argv"]
        assert "-U" in seen["argv"], "re-login must update in place, not fail"

    def test_store_failure_returns_false(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b"", b"locked")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_handles_missing_helper(self, monkeypatch):
        monkeypatch.setattr(keychain, "_tool", lambda: None)
        assert keychain.store("a@b.c", "pw") is False

    def test_store_handles_oserror(self, monkeypatch):
        def boom(argv, **kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(keychain, "_run", boom)
        assert keychain.store("a@b.c", "pw") is False

    def test_lookup_returns_secret(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"the-password\n", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") == "the-password"

    def test_lookup_missing_item_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 44, b"", b"not found")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.lookup("a@b.c") is None

    def test_delete_reports_success(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(keychain, "_run", fake_run)
        assert keychain.delete("a@b.c") is True
        assert "delete-generic-password" in seen["argv"]


class TestKeychainWiring:
    def test_store_prefers_keychain_and_drops_cleartext(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        _write_creds(creds)  # leftover from a previous non-keychain login
        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=True) as store,
        ):
            backend = _store_password("student@example.com", "s3cret")
        assert backend == "keychain"
        store.assert_called_once_with("student@example.com", "s3cret")
        assert not creds.exists(), "password left in cleartext creds.json too"

    def test_store_falls_back_to_file_when_keychain_fails(self, isolated_env):
        """A locked keychain must not silently lose the credential."""
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        with (
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=False),
        ):
            backend = _store_password("student@example.com", "s3cret")
        assert backend == "file"
        assert creds.exists()
        assert json.loads(creds.read_text())["password"] == "s3cret"

    def test_store_uses_file_when_keychain_off(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        with (
            patch.object(keychain, "enabled", return_value=False),
            patch.object(keychain, "store") as store,
        ):
            assert _store_password("student@example.com", "s3cret") == "file"
        store.assert_not_called()
        assert creds.exists()

    def test_load_falls_back_to_keychain_when_file_has_no_password(self, isolated_env, tmp_path):
        tmp_path / "creds.json"
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value="from-keychain") as lookup,
        ):
            creds = _load_creds("student@example.com")
        lookup.assert_called_once_with("student@example.com")
        assert creds == {"email": "student@example.com", "password": "from-keychain"}

    def test_file_password_wins_over_keychain(self, isolated_env, tmp_path):
        _write_creds(tmp_path / "creds.json")
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value="stale") as lookup,
        ):
            creds = _load_creds("student@example.com")
        lookup.assert_not_called()
        assert creds["password"] == "s3cret"

    def test_load_returns_none_without_either_backend(self, isolated_env):
        with (
            patch.object(keychain, "available", return_value=True),
            patch.object(keychain, "lookup", return_value=None),
        ):
            assert _load_creds("student@example.com") is None

    def test_login_keychain_flag_threads_through_build_client(self, isolated_env):
        args = m.build_parser().parse_args(["login", "--keychain", "--temp"])
        assert args.keychain is True
        with patch("mb_cli.auth.ManageBacClient") as client_cls:
            client_cls.return_value.login.return_value = True
            build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
                remember=not getattr(args, "temp", False),
                use_keychain=getattr(args, "keychain", None),
            )

    def test_keychain_enabled_login_writes_no_cleartext(self, isolated_env):
        creds = Path(os.environ["MB_CRAWLER_CREDS_PATH"])
        with (
            patch("mb_cli.auth.ManageBacClient") as client_cls,
            patch.object(keychain, "enabled", return_value=True),
            patch.object(keychain, "store", return_value=True),
        ):
            client_cls.return_value.login.return_value = True
            build_client(
                school="bj80",
                email="student@example.com",
                password="s3cret",
                use_keychain=True,
            )
        assert not creds.exists()


# ── Task 3 — MB_CRAWLER_PASSWORD / MB_CRAWLER_COOKIE are readable ────────


class TestCredentialEnvVars:
    """These were write-only: exported into the daemon child, never read back."""

    def _parse(self, *argv):
        return m.build_parser().parse_args(list(argv))

    def test_password_read_from_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        captured = {}
        args = self._parse("list", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["password"] == "env-password"

    def test_cookie_read_from_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_COOKIE", "env-cookie")
        captured = {}
        args = self._parse("list", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["cookie"] == "env-cookie"

    def test_explicit_password_beats_environment(self, isolated_env, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        captured = {}
        args = self._parse("list", "--password", "flag-password", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        assert captured["password"] == "flag-password"

    def test_environment_avoids_the_interactive_prompt(self, isolated_env, monkeypatch):
        """The point of the env var: no TTY needed in CI."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "env-password")
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_not_called()

    def test_prompt_still_used_without_environment(self, isolated_env):
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass", return_value="typed") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_called_once()

    def test_empty_environment_value_falls_through(self, isolated_env, monkeypatch):
        """An exported-but-empty var must not look like a supplied password."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "")
        args = self._parse("list", "--format", "json")
        with (
            patch.object(m, "build_client", side_effect=SystemExit(0)),
            patch.object(m, "load_state") as load_state,
            patch.object(m.getpass, "getpass", return_value="typed") as getpass,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "list")
        getpass.assert_called_once()

    def test_daemon_start_still_exports_for_the_child(self, isolated_env):
        """Round-trip: the parent exports, and the child can now read it back."""
        args = self._parse(
            "daemon", "start", "-b", "--password", "pw123", "--format", "json"
        )
        with patch(
            "mb_cli.daemon.system.ServiceManager.start_background",
            return_value={"started": True},
        ) as start:
            m.cmd_daemon_start(args)
        _, kwargs = start.call_args
        assert kwargs["env"]["MB_CRAWLER_PASSWORD"] == "pw123"
        assert "pw123" not in " ".join(kwargs["extra_args"])

    def test_daemon_run_child_reads_the_exported_password(self, isolated_env, monkeypatch):
        """The child `mb daemon run` sees MB_CRAWLER_PASSWORD and uses it."""
        monkeypatch.setenv("MB_CRAWLER_PASSWORD", "pw123")
        captured = {}
        args = self._parse("daemon", "run", "--format", "json")

        def fake_build(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        with (
            patch.object(m, "build_client", fake_build),
            patch.object(m, "load_state") as load_state,
        ):
            load_state.return_value = MagicMock(session=MagicMock(cookie=None))
            with pytest.raises(SystemExit):
                m._build_client(args, "daemon")
        assert captured["password"] == "pw123"


# ── config-level helpers ────────────────────────────────────────────────


class TestResolveCredsPath:
    def test_explicit_wins(self, tmp_path):
        assert resolve_creds_path(str(tmp_path / "x.json")) == tmp_path / "x.json"

    def test_env_var(self, isolated_env):
        assert resolve_creds_path() == Path(os.environ["MB_CRAWLER_CREDS_PATH"])

    def test_default_lives_in_config_dir(self, monkeypatch):
        monkeypatch.delenv("MB_CRAWLER_CREDS_PATH", raising=False)
        path = resolve_creds_path()
        assert path.name == "creds.json"
        assert path.parent.name == "tahuti"

    def test_default_is_mode_guarded(self, isolated_env):
        """Sanity: the path we resolve is the one we write 0600."""
        assert resolve_creds_path().name == "creds.json"
