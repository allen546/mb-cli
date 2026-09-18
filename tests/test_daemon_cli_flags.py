"""Tests for the daemon CLI flags that used to be silently dropped.

Each test here pins a defect proven by executing the CLI before it was fixed:

* ``daemon start --interval N`` wrote a config key nothing reads, so the
  interval stayed at the 30s default.
* ``daemon start -b`` forwarded only ``--webhook-url`` and the interval, so
  ``-b --dry-run`` posted real webhooks and ``-b --once`` never exited.
* ``daemon start`` had no ``--secret`` at all, so a foreground start could
  never sign a webhook.
* ``daemon run`` lacked the delivery, interval and active-hours flags that
  ``start`` had.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.__main__ import build_parser, cmd_daemon_run, cmd_daemon_start
from mb_cli.__main__ import _apply_daemon_overrides


class _DaemonArgs:
    def __init__(self, **overrides):
        self.webhook_url = None
        self.secret = None
        self.channel_id = None
        self.recipient = None
        self.poll_interval = None
        self.interval = None
        self.active_hours_start = None
        self.active_hours_end = None
        self.once = False
        self.dry_run = False
        self.daemon_config = None
        self.output = None
        self.format = None
        self.pid_file = None
        self.log_file = None
        self.no_verify_tls = False
        for key, value in overrides.items():
            setattr(self, key, value)


# ── Config translation ───────────────────────────────────────────────────


def test_daemon_start_interval_reaches_poll_interval_seconds():
    """The old code wrote `interval`, which DaemonConfig.from_dict never reads."""
    config = _apply_daemon_overrides({}, _DaemonArgs(interval=45))
    assert config["poll_interval_seconds"] == 45
    assert "interval" not in config


def test_daemon_run_poll_interval_reaches_poll_interval_seconds():
    config = _apply_daemon_overrides({}, _DaemonArgs(poll_interval=90))
    assert config["poll_interval_seconds"] == 90


def test_active_hours_become_active_windows():
    config = _apply_daemon_overrides(
        {}, _DaemonArgs(active_hours_start=7, active_hours_end=23)
    )
    assert config["active_windows"] == [["07:00", "23:00"]]
    assert "active_hours_start" not in config
    assert "active_hours_end" not in config


def test_active_hours_half_specified_gets_documented_defaults():
    config = _apply_daemon_overrides({}, _DaemonArgs(active_hours_start=9))
    assert config["active_windows"] == [["09:00", "23:00"]]

    config = _apply_daemon_overrides({}, _DaemonArgs(active_hours_end=18))
    assert config["active_windows"] == [["07:00", "18:00"]]


def test_no_active_hours_leaves_windows_untouched():
    config = {"active_windows": [["08:00", "20:00"]]}
    _apply_daemon_overrides(config, _DaemonArgs())
    assert config["active_windows"] == [["08:00", "20:00"]]


def test_secret_is_folded_into_the_webhook_entry():
    config = _apply_daemon_overrides(
        {}, _DaemonArgs(webhook_url="https://h.example/x", secret="s3cret")
    )
    assert config["webhooks"][0]["secret"] == "s3cret"
    assert config["webhooks"][0]["url"] == "https://h.example/x"
    assert config["delivery"]["mode"] == "webhook"


def test_channel_delivery_requires_both_ids():
    config = _apply_daemon_overrides({}, _DaemonArgs(channel_id="qq"))
    assert "delivery" not in config

    config = _apply_daemon_overrides(
        {}, _DaemonArgs(channel_id="qq", recipient="42")
    )
    assert config["delivery"] == {
        "mode": "channel_send",
        "channel_id": "qq",
        "recipient": "42",
    }


def test_daemon_config_from_dict_reads_the_translated_interval():
    """End to end through the real config parser, not just the dict."""
    from mb_cli.daemon import DaemonConfig

    config = DaemonConfig.from_dict(
        _apply_daemon_overrides({}, _DaemonArgs(interval=45))
    )
    assert config.poll_interval_seconds == 45


# ── `daemon start -b` argument forwarding ────────────────────────────────


def _background_args(**overrides):
    base = dict(
        background=True,
        profile=None,
        config=None,
        session_file=None,
        school=None,
        domain=None,
        email=None,
        password=None,
        cookie=None,
        daemon_config=None,
        webhook_url=None,
        secret=None,
        channel_id=None,
        recipient=None,
        interval=None,
        poll_interval=None,
        active_hours_start=None,
        active_hours_end=None,
        once=False,
        dry_run=False,
        no_verify_tls=False,
        pid_file=None,
        log_file=None,
        output=None,
        format=None,
    )
    base.update(overrides)
    return _DaemonArgs(**base)


def test_background_forwards_previously_dropped_flags():
    with patch("mb_cli.__main__.ServiceManager") as MockMgr:
        mgr = MockMgr.return_value
        mgr.start_background.return_value = {"started": True, "pid": 1}
        rc = cmd_daemon_start(
            _background_args(
                dry_run=True,
                once=True,
                channel_id="qq",
                recipient="42",
                active_hours_start=7,
                active_hours_end=23,
                interval=45,
            )
        )

    assert rc == 0
    extra = mgr.start_background.call_args[1]["extra_args"]
    # `-b --dry-run` used to POST real webhooks; `-b --once` used to loop forever.
    assert "--dry-run" in extra
    assert "--once" in extra
    assert "--channel-id" in extra
    assert "qq" in extra
    assert "--recipient" in extra
    assert "42" in extra
    assert "--active-hours-start" in extra
    assert "7" in extra
    assert "--active-hours-end" in extra
    assert "23" in extra
    assert "--poll-interval" in extra
    assert "45" in extra


def test_background_secret_goes_to_environment_not_argv():
    with patch("mb_cli.__main__.ServiceManager") as MockMgr:
        mgr = MockMgr.return_value
        mgr.start_background.return_value = {"started": True, "pid": 1}
        cmd_daemon_start(_background_args(webhook_url="https://h/x", secret="s3cret"))

    kwargs = mgr.start_background.call_args[1]
    assert kwargs["env"]["MB_WEBHOOK_SECRET"] == "s3cret"
    assert "s3cret" not in kwargs["extra_args"]


def test_background_reports_failure_exit_code():
    with patch("mb_cli.__main__.ServiceManager") as MockMgr:
        mgr = MockMgr.return_value
        mgr.start_background.return_value = {"started": False, "reason": "already_running"}
        rc = cmd_daemon_start(_background_args())
    assert rc == 1


def test_foreground_start_passes_interval_to_start_loop(tmp_path):
    state = MagicMock()
    state.active_profile = "default"
    client = MagicMock()

    with (
        patch("mb_cli.__main__._build_client", return_value=(state, client, "a@b.com")),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__.load_daemon_config", return_value={}),
        patch("mb_cli.__main__.start_loop", return_value={}) as mock_loop,
        patch("mb_cli.__main__.print_payload"),
    ):
        rc = cmd_daemon_start(_DaemonArgs(interval=45))

    assert rc == 0
    config = mock_loop.call_args[0][1]
    assert config["poll_interval_seconds"] == 45
    assert "interval" not in config


# ── Sibling-command flag parity ──────────────────────────────────────────


def test_daemon_run_and_start_accept_both_interval_spellings():
    parser = build_parser()

    run = parser.parse_args(["daemon", "run", "--interval", "12"])
    assert run.poll_interval == 12

    run = parser.parse_args(["daemon", "run", "--poll-interval", "34"])
    assert run.poll_interval == 34

    start = parser.parse_args(["daemon", "start", "--interval", "12"])
    assert start.interval == 12

    start = parser.parse_args(["daemon", "start", "--poll-interval", "34"])
    assert start.interval == 34


def test_daemon_run_gained_the_flags_only_start_had():
    parser = build_parser()
    args = parser.parse_args(
        [
            "daemon",
            "run",
            "--webhook-url",
            "https://h/x",
            "--secret",
            "s3cret",
            "--channel-id",
            "qq",
            "--recipient",
            "42",
            "--active-hours-start",
            "7",
            "--active-hours-end",
            "23",
            "--dry-run",
            "--once",
        ]
    )
    assert args.webhook_url == "https://h/x"
    assert args.secret == "s3cret"
    assert args.channel_id == "qq"
    assert args.recipient == "42"
    assert args.active_hours_start == 7
    assert args.active_hours_end == 23
    assert args.dry_run is True
    assert args.once is True


def test_daemon_start_gained_the_secret_flag():
    parser = build_parser()
    args = parser.parse_args(["daemon", "start", "--secret", "s3cret"])
    assert args.secret == "s3cret"


def test_daemon_run_forwards_active_hours_to_config():
    """`run` resolves the same keys as `start`, through the shared helper."""
    args = parser_run_args = build_parser().parse_args(
        ["daemon", "run", "--active-hours-start", "9", "--active-hours-end", "18"]
    )
    config = _apply_daemon_overrides({}, args)
    assert config["active_windows"] == [["09:00", "18:00"]]
