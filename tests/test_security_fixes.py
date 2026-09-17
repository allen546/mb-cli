"""Security-regression tests for the audit fixes."""
import pytest
from unittest.mock import MagicMock, patch

from mb_cli import __main__ as m
from mb_cli.client import ManageBacClient, _validate_school_domain
from mb_cli.exceptions import CommandError


class TestSchoolDomainValidation:
    def test_rejects_offsite_school(self):
        with pytest.raises(CommandError) as e:
            _validate_school_domain("evil.com/x", "managebac.com")
        assert e.value.code == "invalid_school"

    def test_rejects_foreign_domain(self):
        with pytest.raises(CommandError) as e:
            _validate_school_domain("bj80", "evil.com")
        assert e.value.code == "invalid_domain"

    def test_rejects_empty_school(self):
        with pytest.raises(CommandError):
            _validate_school_domain("", "managebac.com")

    def test_accepts_valid(self):
        assert _validate_school_domain("bj80", "managebac.cn") == ("bj80", "managebac.cn")

    def test_strips_domain_suffix(self):
        assert _validate_school_domain("bj80.managebac.cn", "managebac.cn") == (
            "bj80",
            "managebac.cn",
        )

    def test_constructor_rejects_offsite(self):
        with pytest.raises(CommandError):
            ManageBacClient(school="evil.com/x")


class TestCrossHostRedirectGuard:
    def test_allows_same_estate(self):
        c = ManageBacClient(school="bj80")
        c._assert_same_host("https://managebac.com/ical/x.ics")  # must not raise

    def test_blocks_foreign_host(self):
        c = ManageBacClient(school="bj80")
        with pytest.raises(CommandError) as e:
            c._assert_same_host("https://evil.example.com/steal")
        assert e.value.code == "cross_host_redirect_blocked"

    def test_blocks_suffix_spoof(self):
        c = ManageBacClient(school="bj80")
        with pytest.raises(CommandError):
            c._assert_same_host("https://managebac.com.evil.net/x")


class TestSafeFilename:
    def test_blocks_traversal(self):
        from mb_cli.__main__ import _safe_filename
        assert _safe_filename("../../../../etc/passwd") == "passwd"
        assert "/" not in _safe_filename("a/b/c.pdf")

    def test_strips_dots(self):
        from mb_cli.__main__ import _safe_filename
        assert _safe_filename("..") == "download"

    def test_keeps_normal_name(self):
        from mb_cli.__main__ import _safe_filename
        assert _safe_filename("homework 1.pdf") == "homework 1.pdf"


class TestStateEvictionOrder:
    def test_evicts_oldest_not_newest(self):
        from mb_cli.daemon.state import DaemonStateManager
        m = DaemonStateManager.__new__(DaemonStateManager)
        m.dispatched_reminders = {}
        # Insert 9 then 10 — lexicographic sort would evict "task_10" first.
        m.mark_reminder_dispatched(9, "24h")
        m.mark_reminder_dispatched(10, "24h")
        assert list(m.dispatched_reminders)[0] == "task_9:ddl_24h"

    def test_legacy_list_format_migrates(self, tmp_path):
        import json
        from mb_cli.daemon.state import DaemonStateManager
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"dispatched_reminders": ["task_1:ddl_1h"]}))
        m = DaemonStateManager(state_path=p)
        assert m.is_reminder_dispatched(1, "1h")
        assert isinstance(m.dispatched_reminders, dict)


def _parse_daemon_start(*extra):
    return m.build_parser().parse_args(["daemon", "start", *extra])


class TestBackgroundDaemonSecretEnv:
    """`daemon start --background` must pass credentials via the environment.

    argv is world-readable via `ps` for the life of the daemon, so leaving the
    password, cookie, or HMAC secret there leaks it to any local user.
    """

    def _start(self, args):
        with patch("mb_cli.daemon.system.ServiceManager.start_background") as start:
            start.return_value = {"started": True}
            m.cmd_daemon_start(args)
        assert start.called, "start_background was not called"
        _, kwargs = start.call_args
        return kwargs.get("extra_args") or [], kwargs.get("env") or {}

    def test_password_goes_via_env_not_argv(self):
        extra, env = self._start(
            _parse_daemon_start("--background", "--password", "pw123")
        )
        assert env.get("MB_CRAWLER_PASSWORD") == "pw123"
        assert "pw123" not in " ".join(extra)

    def test_cookie_goes_via_env_not_argv(self):
        extra, env = self._start(
            _parse_daemon_start("--background", "--cookie", "cookieval")
        )
        assert env.get("MB_CRAWLER_COOKIE") == "cookieval"
        assert "cookieval" not in " ".join(extra)

    def test_background_start_with_credential_does_not_raise(self):
        """Regression: daemon_secret_env was used before it was assigned.

        The original fix built ``daemon_secret_env`` *after* the --password and
        --cookie branches that write into it, so any background start carrying a
        credential raised NameError instead of starting the daemon.
        """
        for argv in (
            ("--password", "pw123"),
            ("--cookie", "cookieval"),
            ("--password", "pw123", "--cookie", "cookieval"),
        ):
            extra, env = self._start(_parse_daemon_start("--background", *argv))
            assert not any(v in " ".join(extra) for v in ("pw123", "cookieval"))


class TestWebhookSecretFromEnv:
    """A background-spawned daemon must sign with the secret it was handed.

    ``daemon start --background`` forwards the secret as MB_WEBHOOK_SECRET, so
    the child must read it from the environment. Reading argv only left the
    daemon signing with an empty secret, and the receiver rejects every push.
    """

    def _captured_secret(self, env_secret):
        captured = {}

        def fake_start_loop(client, cfg, **kwargs):
            captured["secret"] = cfg["webhooks"][0]["secret"]
            raise SystemExit(0)  # stop before print_payload serialises mocks

        args = _parse_daemon_start("--webhook-url", "http://127.0.0.1:42617/webhook")
        args.dry_run = True
        with (
            patch.object(m, "start_loop", fake_start_loop),
            patch.object(
                m, "_build_client", return_value=(MagicMock(), MagicMock(), "default")
            ),
            patch.object(m, "_authenticate_client", lambda *a, **k: None),
        ):
            if env_secret is None:
                import os

                with patch.dict("os.environ", {}, clear=False):
                    os.environ.pop("MB_WEBHOOK_SECRET", None)
                    try:
                        m.cmd_daemon_start(args)
                    except SystemExit:
                        pass
            else:
                with patch.dict("os.environ", {"MB_WEBHOOK_SECRET": env_secret}):
                    try:
                        m.cmd_daemon_start(args)
                    except SystemExit:
                        pass
        return captured.get("secret")

    def test_daemon_start_reads_secret_from_environment(self):
        assert self._captured_secret("shared-secret") == "shared-secret"

    def test_daemon_start_without_env_secret_signs_nothing(self):
        assert self._captured_secret(None) is None

    def test_resolve_secret_prefers_cli_then_env(self):
        import os

        from mb_cli.daemon import _resolve_secret

        with patch.dict("os.environ", {"MB_WEBHOOK_SECRET": "env-secret"}):
            assert _resolve_secret("cli-secret") == "cli-secret"
            assert _resolve_secret(None) == "env-secret"
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("MB_WEBHOOK_SECRET", None)
            assert _resolve_secret(None) is None
