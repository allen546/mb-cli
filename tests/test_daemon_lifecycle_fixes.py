"""Regression tests for the daemon lifecycle defects.

Every test here fails against the code as it was before the fixes: the 679-test
suite was green while all of these were broken, so "the suite passes" was never
evidence that they worked.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests_mock as rm

from tahuti.daemon import (
    DaemonConfig,
    DaemonService,
    _is_in_window,
    _next_active_window,
    configure_channel_send,
    configure_webhook,
    load_daemon_config,
    make_auth_refresh_fn,
    normalize_active_windows,
    save_daemon_config,
    start_loop,
    stop_daemon,
)
from tahuti.daemon.events import MBEvent, ReminderThreshold
from tahuti.daemon.provider import AbstractNotificationProvider
from tahuti.daemon import state as state_module
from tahuti.daemon.scheduler import DDLScheduler, resolve_school_timezone
from tahuti.daemon.state import DaemonStateManager
from tahuti.daemon.system import (
    ServiceManager,
    pid_alive,
    read_pid_file,
    write_pid_file,
)

# ── helpers ───────────────────────────────────────────────────────────────


class _Provider(AbstractNotificationProvider):
    """Provider that hands out whatever ``make`` builds, every cycle."""

    def __init__(self, make):
        self._make = make

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def refresh_auth(self) -> bool:
        return True

    def poll_events(self):
        return self._make()


def _static_provider(events: list[MBEvent]) -> _Provider:
    """A provider that returns fresh copies of ``events`` on every poll.

    ``run_check_cycle`` mutates the events it processes, and the defect being
    tested is precisely "the provider keeps returning the same item", so each
    cycle has to get an untouched one.
    """

    def make():
        return [copy.deepcopy(ev) for ev in events]

    return _Provider(make)


def _client(tmp_path: Path) -> MagicMock:
    client = MagicMock()
    client.base = "https://school.managebac.cn"
    client.get_tasks_by_view.return_value = []
    return client


def _service(tmp_path: Path, provider=None, **service_kwargs) -> DaemonService:
    config_kwargs = service_kwargs.pop("config", {})
    return DaemonService(
        client=_client(tmp_path),
        config=DaemonConfig(**config_kwargs),
        state_manager=DaemonStateManager(tmp_path / "state.json"),
        provider=provider or _Provider(lambda: []),
        **service_kwargs,
    )


def _daemon_config(tmp_path: Path, **extra) -> dict:
    cfg = {
        "delivery": {"mode": "webhook", "webhook_url": "http://localhost:9999/webhook"},
        "snapshot_file": str(tmp_path / "snapshot.json"),
        "pid_file": str(tmp_path / "daemon.pid"),
        "log_file": str(tmp_path / "daemon.log"),
        "active_windows": [],
    }
    cfg.update(extra)
    return cfg


def _stop_after(service: DaemonService, cycles: int = 1):
    """Patch time.sleep so the daemon loop exits after ``cycles`` iterations."""
    counter = {"n": 0}

    def _sleep(_seconds):
        counter["n"] += 1
        if counter["n"] >= cycles:
            service._running = False

    return patch("tahuti.daemon.service.time.sleep", side_effect=_sleep)


@pytest.fixture(autouse=True)
def _isolate_default_state_path(tmp_path, monkeypatch):
    """No test here may read or write the operator's real daemon state file.

    ``DaemonService`` falls back to ``DaemonStateManager()`` — the default path —
    when no state manager is passed, so the default has to point somewhere
    disposable for every test in this module.
    """
    monkeypatch.setattr(
        state_module, "DEFAULT_STATE_PATH", tmp_path / "default_daemon_state.json"
    )


def _service_factory(tmp_path: Path, provider=None):
    """Patch DaemonService so ``start_loop`` gets a hermetic service.

    Two things get swapped: the provider (the real MNNHubProvider needs a live
    notification token, which no test may fetch) and the state manager (the
    default one reads the operator's real daemon_state.json).
    """
    real = DaemonService
    state_path = tmp_path / "state.json"

    def factory(client_arg, **kwargs):
        service = real(client_arg, **kwargs)
        if provider is not None:
            service.provider = provider
        else:
            service.provider = _Provider(lambda: [])
        service.state_manager = DaemonStateManager(state_path)
        return service

    return patch("tahuti.daemon.DaemonService", side_effect=factory)


# ── 1. the long-running path must publish a stoppable pid ─────────────────


def test_service_writes_a_0600_pid_file_for_the_life_of_the_loop(tmp_path: Path):
    """`daemon run` is the ExecStart of the generated unit; it wrote no pid."""
    pid_path = tmp_path / "daemon.pid"
    service = _service(
        tmp_path, config={"poll_interval_seconds": 1, "poll_jitter_seconds": 0}
    )
    service.pid_file = pid_path

    observed: dict[str, str] = {}

    def _sleep(_seconds):
        observed["pid"] = pid_path.read_text(encoding="utf-8").strip()
        observed["mode"] = oct(pid_path.stat().st_mode & 0o777)
        service._running = False

    with patch("tahuti.daemon.service.time.sleep", side_effect=_sleep):
        service.start()

    assert observed["pid"] == str(os.getpid())
    assert observed["mode"] == "0o600"
    # ...and it cleans up after itself on the way out.
    assert not pid_path.exists()


def _spawn(code: str) -> subprocess.Popen:
    """Spawn a child that announces readiness, so tests never race its startup."""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline().strip()
    assert ready == "READY", f"child did not come up: {ready!r}"
    return proc


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def test_start_loop_publishes_a_pid_a_stop_can_find(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    observed: dict[str, str] = {}

    def _on_start(svc):
        observed["pid"] = pid_path.read_text(encoding="utf-8").strip()
        observed["mode"] = oct(pid_path.stat().st_mode & 0o777)
        svc._running = False

    with _service_factory(tmp_path):
        result = start_loop(
            _client(tmp_path), _daemon_config(tmp_path), once=False, on_start=_on_start
        )

    assert observed["pid"] == str(os.getpid())
    assert observed["mode"] == "0o600"
    assert result == {"stopped": True}
    assert not pid_path.exists()


def test_a_daemon_pid_written_by_the_service_can_be_stopped(tmp_path: Path):
    """End-to-end for defect 1: the pid file makes the process stoppable."""
    pid_path = tmp_path / "daemon.pid"
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

    proc = _spawn("import time; print('READY', flush=True); time.sleep(60)")
    # Exactly what DaemonService._acquire_pid_file does.
    write_pid_file(pid_path, proc.pid)
    assert read_pid_file(pid_path) == proc.pid
    assert pid_alive(proc.pid) is True

    try:
        with patch("tahuti.daemon._is_tahuti_pid", return_value=True):
            result = stop_daemon(str(config_path))
        assert result["stopped"] is True
        assert result["pid"] == proc.pid
        # The claim has to match reality: the process is actually gone.
        proc.wait(timeout=10)
        assert pid_alive(proc.pid) is False
        assert not pid_path.exists()
    finally:
        _reap(proc)


# ── 2. `--once` must actually deliver ─────────────────────────────────────


def _run_once_with_provider(tmp_path: Path, provider, dry_run: bool, client=None):
    """Drive start_loop(once=True) with an injected provider."""
    daemon_config = _daemon_config(
        tmp_path, webhooks=[{"url": "http://localhost:9999/webhook", "events": ["*"]}]
    )
    captured: dict[str, object] = {}

    def factory(client_arg, **kwargs):
        service = DaemonService(client_arg, **kwargs)
        service.provider = provider
        service.state_manager = DaemonStateManager(tmp_path / "state.json")
        captured["service"] = service
        return service

    with patch("tahuti.daemon.DaemonService", side_effect=factory):
        result = start_loop(
            client or _client(tmp_path),
            daemon_config,
            once=True,
            dry_run=dry_run,
        )
    return result, captured


def test_once_path_dispatches_through_the_service(tmp_path: Path):
    """`daemon start --once --webhook-url …` used to send nothing and say so."""
    provider = _static_provider(
        [
            MBEvent(
                event="task_created",
                data={"notification_id": 4242, "title": "History Essay"},
            )
        ]
    )
    client = _client(tmp_path)
    service_holder: dict[str, DaemonService] = {}

    daemon_config = _daemon_config(
        tmp_path, webhooks=[{"url": "http://localhost:9999/webhook", "events": ["*"]}]
    )

    def factory(client_arg, **kwargs):
        service = DaemonService(client_arg, **kwargs)
        service.provider = provider
        service.state_manager = DaemonStateManager(tmp_path / "state.json")
        service_holder["service"] = service
        return service

    with rm.Mocker() as m, patch("tahuti.daemon.DaemonService", side_effect=factory):
        m.post("http://localhost:9999/webhook", status_code=200)
        result = start_loop(client, daemon_config, once=True, dry_run=False)

    assert result["alert_count"] == 1
    assert result["new_notifications"] == 1
    assert result["delivered"] is True
    assert result["alerts"][0]["message"]
    # The webhook really was POSTed.
    assert m.call_count == 1
    assert service_holder["service"].state_manager.is_notification_processed(4242)
    # The old path crawled the index and diffed a snapshot instead of polling.
    client.crawl_index.assert_not_called()


def test_once_dry_run_reports_that_nothing_was_delivered(tmp_path: Path):
    provider = _static_provider(
        [
            MBEvent(
                event="task_created",
                data={"notification_id": 4243, "title": "Dry Run Essay"},
            )
        ]
    )
    with rm.Mocker() as m:
        m.post("http://localhost:9999/webhook", status_code=200)
        result, _ = _run_once_with_provider(tmp_path, provider, dry_run=True)

    assert result["alert_count"] == 1  # it computed the alert...
    assert result["dry_run"] is True
    assert result["delivered"] is False  # ...and told the truth about sending it
    assert m.call_count == 0


def test_once_result_is_json_serialisable(tmp_path: Path):
    """The CLI json-dumps this payload; MBEvent objects are not serialisable."""
    provider = _static_provider(
        [MBEvent(event="task_created", data={"notification_id": 4244, "title": "T"})]
    )
    result, _ = _run_once_with_provider(tmp_path, provider, dry_run=True)
    json.dumps(result)


# ── 3. the foreground start path needs an auth refresh callback ───────────


def test_start_loop_threads_auth_refresh_fn_into_the_service(tmp_path: Path):
    sentinel = MagicMock(return_value=True)
    captured: dict[str, object] = {}
    real = DaemonService

    def factory(client_arg, **kwargs):
        captured.update(kwargs)
        service = real(client_arg, **kwargs)
        service.provider = _Provider(lambda: [])
        service.state_manager = DaemonStateManager(tmp_path / "state.json")
        return service

    with patch("tahuti.daemon.DaemonService", side_effect=factory):
        start_loop(
            _client(tmp_path),
            _daemon_config(tmp_path),
            once=True,
            auth_refresh_fn=sentinel,
        )

    assert captured["auth_refresh_fn"] is sentinel


def test_make_auth_refresh_fn_reports_failure_instead_of_raising(tmp_path: Path):
    client = MagicMock()
    with patch(
        "tahuti.auth._relogin_from_creds", side_effect=RuntimeError("no creds")
    ) as relogin:
        assert make_auth_refresh_fn(client, MagicMock())() is False
        relogin.assert_called_once()

    with patch("tahuti.auth._relogin_from_creds") as relogin:
        assert make_auth_refresh_fn(client, MagicMock())() is True
        relogin.assert_called_once()


# ── 4. one malformed notification_id must not poison the cycle ────────────


def test_malformed_notification_id_does_not_poison_the_batch(tmp_path: Path):
    provider = _static_provider(
        [
            MBEvent(
                event="task_created",
                data={"notification_id": "not-a-number", "title": "Bad"},
            ),
            MBEvent(
                event="task_created",
                data={"notification_id": 2001, "title": "Good"},
            ),
        ]
    )
    service = _service(tmp_path, provider=provider)
    dispatched: list[MBEvent] = []
    service.dispatcher.dispatch = MagicMock(
        side_effect=lambda ev: dispatched.append(ev) or [{"success": True}]
    )

    first = service.run_check_cycle()
    # The good event survives even though it sits behind the bad one.
    assert first["new_notifications"] == 1
    assert dispatched[0].data["title"] == "Good"

    # ...and the bad one is suppressed rather than re-warned about forever.
    second = service.run_check_cycle()
    assert second["new_notifications"] == 0
    assert len(dispatched) == 1


def test_numeric_string_notification_ids_are_still_deduplicated(tmp_path: Path):
    provider = _static_provider(
        [MBEvent(event="task_created", data={"notification_id": "2002", "title": "T"})]
    )
    service = _service(tmp_path, provider=provider)
    service.dispatcher.dispatch = MagicMock(return_value=[{"success": True}])

    assert service.run_check_cycle()["new_notifications"] == 1
    assert service.state_manager.is_notification_processed(2002)
    assert service.run_check_cycle()["new_notifications"] == 0


# ── 5. an event with no notification_id must not be re-POSTed forever ─────


def test_event_without_notification_id_is_not_redispatched(tmp_path: Path):
    provider = _static_provider(
        [MBEvent(event="task_created", data={"title": "No Id Essay"})]
    )
    service = _service(tmp_path, provider=provider)
    dispatched: list[MBEvent] = []
    service.dispatcher.dispatch = MagicMock(
        side_effect=lambda ev: dispatched.append(ev) or [{"success": True}]
    )

    for _ in range(3):
        service.run_check_cycle()

    assert len(dispatched) == 1, "the same id-less event was re-POSTed every cycle"
    # provider.py builds event_id="notif_None" for these, which is one shared
    # string for every such notification; the service has to give it a real id.
    assert dispatched[0].event_id != "notif_None"
    assert dispatched[0].event_id


def test_id_less_event_id_is_stable_across_cycles(tmp_path: Path):
    provider = _static_provider(
        [MBEvent(event="task_created", data={"title": "Stable", "created_at": "t0"})]
    )
    service = _service(tmp_path, provider=provider)
    seen: list[str] = []
    service.dispatcher.dispatch = MagicMock(
        side_effect=lambda ev: seen.append(ev.event_id) or [{"success": True}]
    )
    service.run_check_cycle()
    service.run_check_cycle()
    assert len(seen) == 1


# ── 6. an empty "upcoming" view must not mean a per-cycle full crawl ──────


def test_empty_upcoming_sync_does_not_recrawl_every_cycle(tmp_path: Path):
    client = _client(tmp_path)
    client.get_tasks_by_view.return_value = []
    service = DaemonService(
        client=client,
        config=DaemonConfig(poll_interval_seconds=1, poll_jitter_seconds=0),
        state_manager=DaemonStateManager(tmp_path / "state.json"),
        provider=_Provider(lambda: []),
    )

    with _stop_after(service, cycles=4):
        service.start()

    # One sync at startup — not one per loop iteration.
    assert client.get_tasks_by_view.call_count == 1


def test_failed_sync_backs_off_instead_of_retrying_every_cycle(tmp_path: Path):
    client = _client(tmp_path)
    client.get_tasks_by_view.side_effect = RuntimeError("connection reset")
    service = DaemonService(
        client=client,
        config=DaemonConfig(poll_interval_seconds=1, poll_jitter_seconds=0),
        state_manager=DaemonStateManager(tmp_path / "state.json"),
        provider=_Provider(lambda: []),
    )

    with _stop_after(service, cycles=4):
        service.start()

    assert client.get_tasks_by_view.call_count == 1
    assert service._sync_failures == 1


def test_full_sync_gate_measures_from_the_last_attempt(tmp_path: Path):
    service = _service(tmp_path)
    # Never attempted → due now.
    assert service._full_sync_due(time.time()) is True

    service._last_sync_attempt = time.time()
    service._sync_failures = 0
    # Successful sync 12h ago → due again.
    service._last_sync_attempt = time.time() - 43201
    assert service._full_sync_due(time.time()) is True

    # Successful sync a moment ago → not due, even though the cache is empty.
    service._last_sync_attempt = time.time()
    assert service._full_sync_due(time.time()) is False

    # Failed sync a moment ago → not due (backoff), but due once the backoff
    # has elapsed.
    service._sync_failures = 1
    assert service._full_sync_due(time.time()) is False
    service._last_sync_attempt = time.time() - 301
    assert service._full_sync_due(time.time()) is True


# ── 7. the next active window must be the earliest one ────────────────────


def test_next_active_window_returns_the_earliest_later_window():
    cfg = {"active_windows": [["22:00", "23:00"], ["12:00", "13:00"]]}
    with patch("tahuti.daemon._now_local", return_value=datetime(2026, 9, 19, 10, 0)):
        nxt = _next_active_window(cfg)
    assert (nxt.hour, nxt.minute) == (12, 0)


def test_next_active_window_falls_back_to_tomorrows_earliest_start():
    cfg = {"active_windows": [["22:00", "23:00"], ["08:00", "09:00"]]}
    with patch("tahuti.daemon._now_local", return_value=datetime(2026, 9, 19, 23, 30)):
        nxt = _next_active_window(cfg)
    assert (nxt.hour, nxt.minute) == (8, 0)
    assert nxt.day == 20


def test_window_end_is_exclusive():
    """09:00-17:00 must stop polling at 17:00, not poll the boundary minute."""
    assert _is_in_window(dt_time(9, 0), dt_time(9, 0), dt_time(17, 0)) is True
    assert _is_in_window(dt_time(16, 59), dt_time(9, 0), dt_time(17, 0)) is True
    assert _is_in_window(dt_time(17, 0), dt_time(9, 0), dt_time(17, 0)) is False
    # Windows that wrap midnight: the end is still exclusive.
    assert _is_in_window(dt_time(23, 0), dt_time(22, 0), dt_time(2, 0)) is True
    assert _is_in_window(dt_time(1, 59), dt_time(22, 0), dt_time(2, 0)) is True
    assert _is_in_window(dt_time(2, 0), dt_time(22, 0), dt_time(2, 0)) is False


# ── 8. a malformed active window must fail open, not kill the daemon ──────


def test_type_mismatched_window_fails_open(tmp_path: Path):
    """`[[7, 23]]` used to raise AttributeError out of the polling loop."""
    service = _service(tmp_path, config={"active_windows": [[7, 23]]})
    assert service._in_active_window() is True


def test_loop_survives_a_type_mismatched_window(tmp_path: Path):
    service = _service(
        tmp_path,
        config={
            "active_windows": [[7, 23]],
            "poll_interval_seconds": 1,
            "poll_jitter_seconds": 0,
        },
    )
    with _stop_after(service, cycles=2):
        service.start()  # must not raise
    assert service._running is False


def test_load_daemon_config_normalizes_hand_written_windows(tmp_path: Path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {
                "active_windows": [
                    [7, 23],
                    ["7:00", "23:00"],
                    ["garbage", "stuff"],
                    ["only-one"],
                ]
            }
        )
    )
    cfg = load_daemon_config(str(path))
    assert cfg["active_windows"] == [["07:00", "23:00"], ["07:00", "23:00"]]
    # Whatever survives must be usable by DaemonConfig/`_parse_window`.
    parsed = DaemonConfig.from_dict(cfg)
    assert parsed.active_windows == [["07:00", "23:00"], ["07:00", "23:00"]]


def test_normalize_active_windows_drops_everything_unusable():
    assert normalize_active_windows([["nope", "nope"]]) == []
    assert normalize_active_windows("nonsense") == []
    assert normalize_active_windows(None) == []


# ── 9. `stop_daemon` must verify termination ──────────────────────────────

_SIGTERM_PROOF_CHILD = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('READY', flush=True)\n"
    "time.sleep(120)\n"
)


def test_stop_daemon_escalates_to_sigkill(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"pid_file": str(pid_path)}))

    proc = _spawn(_SIGTERM_PROOF_CHILD)
    write_pid_file(pid_path, proc.pid)
    try:
        with patch("tahuti.daemon._is_tahuti_pid", return_value=True):
            result = stop_daemon(str(config_path))
        assert result["stopped"] is True
        assert result["escalated_to_sigkill"] is True
        # A daemon that ignores SIGTERM used to be reported stopped while it
        # was still running.
        proc.wait(timeout=10)
        assert pid_alive(proc.pid) is False
        assert not pid_path.exists()
    finally:
        _reap(proc)


def test_stop_daemon_reports_failure_when_the_process_survives(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"pid_file": str(pid_path)}))
    write_pid_file(pid_path, 12345)

    with (
        patch("tahuti.daemon._is_tahuti_pid", return_value=True),
        patch(
            "tahuti.daemon.terminate_pid",
            return_value={"exited": False, "escalated": True},
        ),
    ):
        result = stop_daemon(str(config_path))

    assert result["stopped"] is False
    assert result["reason"] == "did_not_exit"
    # The daemon is still running, so its pid file must survive for the next try.
    assert read_pid_file(pid_path) == 12345


def test_stop_daemon_leaves_a_replaced_pid_file_alone(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"pid_file": str(pid_path)}))
    write_pid_file(pid_path, 12345)

    def _replace_then_report(pid, **_kwargs):
        write_pid_file(pid_path, 999999)  # a `daemon start` landed mid-stop
        return {"exited": True, "escalated": False}

    with (
        patch("tahuti.daemon._is_tahuti_pid", return_value=True),
        patch("tahuti.daemon.terminate_pid", side_effect=_replace_then_report),
    ):
        result = stop_daemon(str(config_path))

    assert result["stopped"] is True
    assert read_pid_file(pid_path) == 999999


# ── 10. daemon.json must never exist world-readable with the secret ───────


def test_save_daemon_config_writes_the_secret_0600(tmp_path: Path):
    path = tmp_path / "daemon.json"
    save_daemon_config(
        {"webhooks": [{"url": "http://x", "secret": "s3cr3t"}]}, str(path)
    )
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_save_daemon_config_leaves_no_plaintext_file_on_failure(
    tmp_path: Path, monkeypatch
):
    """The old write_text-then-chmod order exposed the secret on a crash."""
    path = tmp_path / "daemon.json"
    data = {"webhooks": [{"url": "http://x", "secret": "s3cr3t"}]}

    def _boom(*_args, **_kwargs):
        raise OSError("simulated crash between write and chmod")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        save_daemon_config(data, str(path))

    assert not path.exists(), "the secret was written before permissions were set"
    # `is_file()` because the config dir itself is created under tmp_path, and
    # reading a directory raises rather than failing the assertion.
    leaked = [
        p for p in tmp_path.iterdir() if p.is_file() and "s3cr3t" in p.read_text()
    ]
    assert leaked == [], f"plaintext secret left behind in {leaked}"


# ── 11. start_background must not leak the log fd or lie about starting ───


def test_start_background_closes_the_log_fd_when_popen_fails(
    tmp_path: Path, monkeypatch
):
    mgr = ServiceManager(
        pid_path=tmp_path / "daemon.pid", log_path=tmp_path / "daemon.log"
    )
    opened: list[int] = []
    real_open = os.open

    def _tracking_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        opened.append(fd)
        return fd

    def _boom(*_args, **_kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr("tahuti.daemon.system.os.open", _tracking_open)
    monkeypatch.setattr("tahuti.daemon.system.subprocess.Popen", _boom)

    with pytest.raises(OSError):
        mgr.start_background()

    assert opened, "the log file was never opened"
    for fd in opened:
        with pytest.raises(OSError):
            fcntl.fcntl(fd, fcntl.F_GETFD)


def test_start_background_does_not_report_a_dead_child_as_started(
    tmp_path: Path, monkeypatch
):
    pid_path = tmp_path / "daemon.pid"
    mgr = ServiceManager(pid_path=pid_path, log_path=tmp_path / "daemon.log")

    class _DeadChild:
        pid = 4242
        returncode = 2

        def poll(self):
            return 2

    monkeypatch.setattr(
        "tahuti.daemon.system.subprocess.Popen", lambda *a, **k: _DeadChild()
    )
    result = mgr.start_background()

    assert result["started"] is False
    assert result["reason"] == "child_exited"
    assert not pid_path.exists()


# ── 12. stop must not unlink a pid file it does not own ───────────────────


def test_stop_background_leaves_a_concurrently_written_pid_file(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    mgr = ServiceManager(pid_path=pid_path, log_path=tmp_path / "daemon.log")
    proc = _spawn("import time; print('READY', flush=True); time.sleep(60)")
    write_pid_file(pid_path, proc.pid)

    def _new_daemon_starts_midway(pid, **_kwargs):
        write_pid_file(pid_path, 999999)
        return {"exited": True, "escalated": False}

    try:
        with patch(
            "tahuti.daemon.system.terminate_pid", side_effect=_new_daemon_starts_midway
        ):
            result = mgr.stop_background(verify_process=False)

        assert result["stopped"] is True
        # The finishing stop must not unlink the new daemon's pid file.
        assert read_pid_file(pid_path) == 999999
    finally:
        _reap(proc)


# ── 13. naive due dates are school-local, not host-local ──────────────────


def test_naive_due_date_is_read_on_the_configured_school_clock(tmp_path: Path):
    state = DaemonStateManager(tmp_path / "state.json")
    # 11:59 PM == 23:59 on the school's clock.
    state.update_task(
        {
            "id": "1000099",
            "class_id": "1000012",
            "title": "Essay",
            "due_date": "September 15, 2026 at 11:59 PM",
            "status": "not-submitted",
        }
    )
    reminders = [ReminderThreshold(threshold_minutes=60, name="1h")]
    # 23:00 UTC is 07:00 the next day in UTC+8, so a 23:59 school deadline is
    # 59 minutes away — not ~16 hours away as the host-clock reading claims.
    now = datetime(2026, 9, 15, 15, 0, tzinfo=ZoneInfo("UTC"))
    school_aware = DDLScheduler(state, reminders, school_timezone="Asia/Shanghai")
    events = school_aware.evaluate_deadlines(now=now)
    assert len(events) == 1
    assert events[0].data["due_iso"].endswith("+08:00")

    host_clock = DDLScheduler(state, reminders)
    assert host_clock.evaluate_deadlines(now=now) == []


def test_resolve_school_timezone_rejects_junk():
    assert resolve_school_timezone(None) is None
    assert resolve_school_timezone("") is None
    assert resolve_school_timezone("Mars/Olympus") is None
    assert resolve_school_timezone(7) is None
    assert str(resolve_school_timezone("Asia/Shanghai")) == "Asia/Shanghai"


def test_school_timezone_reaches_the_service_scheduler(tmp_path: Path):
    service = _service(tmp_path, daemon_config={"school_timezone": "Asia/Shanghai"})
    assert str(service.school_timezone) == "Asia/Shanghai"
    assert str(service.scheduler.school_timezone) == "Asia/Shanghai"

    default = _service(tmp_path)
    assert default.school_timezone is None


# ── 14. state: bounded cache and no pointless rewrites ────────────────────


def test_bound_tasks_cache_evicts_the_oldest_entries(tmp_path: Path, monkeypatch):
    state = DaemonStateManager(tmp_path / "state.json")
    clock = [1_000_000.0]
    monkeypatch.setattr("tahuti.daemon.state.time.time", lambda: clock[0])

    for i in range(600):
        state.update_task({"id": f"t{i:03d}", "title": f"Task {i}"})
        clock[0] -= 1.0  # each later insertion is OLDER than the one before

    evicted = state.bound_tasks_cache(max_entries=500)
    assert evicted == 100
    assert len(state.tasks_cache) == 500
    assert state.get_task("t000") is not None, "the newest entry was evicted"
    assert state.get_task("t599") is None, "the oldest entry was kept"


def test_save_bounds_the_tasks_cache(tmp_path: Path):
    state = DaemonStateManager(tmp_path / "state.json")
    for i in range(600):
        state.update_task({"id": f"t{i:03d}", "title": f"Task {i}"})
    state.save()

    saved = json.loads((tmp_path / "state.json").read_text())
    assert len(saved["tasks_cache"]) == 500


def test_save_skips_the_rewrite_when_nothing_changed(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state = DaemonStateManager(state_file)
    state.update_task({"id": "1", "title": "T"})
    state.save()
    first = state_file.stat().st_mtime_ns

    time.sleep(0.02)
    state.save()
    assert state_file.stat().st_mtime_ns == first, "an unchanged state was rewritten"

    state.mark_notification_processed(7)
    state.save()
    assert state_file.stat().st_mtime_ns != first


def test_save_survives_mixed_numeric_and_synthetic_notification_ids(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state = DaemonStateManager(state_file)
    state.mark_notification_processed(2001)
    state.mark_notification_processed("syn_deadbeefdeadbeef")
    state.save()

    reloaded = DaemonStateManager(state_file)
    assert reloaded.is_notification_processed(2001)
    assert reloaded.is_notification_processed("syn_deadbeefdeadbeef")


def test_update_task_does_not_pollute_cached_tasks_with_bookkeeping_keys(
    tmp_path: Path,
):
    state = DaemonStateManager(tmp_path / "state.json")
    state.update_task({"id": "1", "title": "T"})
    assert state.get_task("1") == {"id": "1", "title": "T"}


def test_legacy_inline_cached_at_is_migrated_out_of_the_task(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "tasks_cache": {
                    "1": {"id": "1", "title": "T", "_cached_at": "2020-01-01T00:00:00"}
                }
            }
        )
    )
    state = DaemonStateManager(state_file)
    assert "_cached_at" not in state.get_task("1")
    assert state.task_cached_at["1"] > 0


# ── 15. start_loop's pid file must not be world-readable ──────────────────


def test_once_pid_file_is_0600(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    observed: dict[str, str] = {}

    def factory(client_arg, **kwargs):
        service = DaemonService(client_arg, **kwargs)
        # Read the mode from inside construction, i.e. while the pid file is
        # still on disk.
        observed["mode"] = oct(pid_path.stat().st_mode & 0o777)
        service.provider = _Provider(lambda: [])
        service.state_manager = DaemonStateManager(tmp_path / "state.json")
        return service

    with patch("tahuti.daemon.DaemonService", side_effect=factory):
        start_loop(_client(tmp_path), _daemon_config(tmp_path), once=True)

    assert observed["mode"] == "0o600"
    assert not pid_path.exists()


def test_once_pid_file_is_not_mistaken_for_a_daemon(tmp_path: Path):
    """A one-shot run must never leave a pid `daemon stop` would signal."""
    pid_path = tmp_path / "daemon.pid"
    config_path = tmp_path / "daemon.json"
    config_path.write_text(json.dumps({"pid_file": str(pid_path)}))
    stop_result: dict[str, object] = {}

    def factory(client_arg, **kwargs):
        service = DaemonService(client_arg, **kwargs)
        service.provider = _Provider(lambda: [])
        service.state_manager = DaemonStateManager(tmp_path / "state.json")
        stop_result.update(stop_daemon(str(config_path)))
        return service

    with patch("tahuti.daemon.DaemonService", side_effect=factory):
        start_loop(_client(tmp_path), _daemon_config(tmp_path), once=True)

    assert stop_result["stopped"] is False
    assert stop_result["reason"] == "invalid_pid"


# ── routed in: run_check_cycle must report a failed poll in band ───────────


def test_check_cycle_reports_a_failed_poll(tmp_path: Path):
    """A cycle that could not poll is not a cycle that found nothing."""

    class _Exploding(_Provider):
        def poll_events(self):
            raise RuntimeError("Session expired or invalid")

    service = _service(tmp_path, provider=_Exploding(lambda: []))
    result = service.run_check_cycle()

    assert result["new_notifications"] == 0
    assert "Session expired" in result["poll_error"]
    assert result["deadline_error"] is None


def test_check_cycle_reports_no_poll_error_on_success(tmp_path: Path):
    service = _service(tmp_path, provider=_Provider(lambda: []))
    result = service.run_check_cycle()
    assert result["poll_error"] is None
    assert result["deadline_error"] is None


def test_once_result_carries_the_poll_error(tmp_path: Path):
    class _Exploding(_Provider):
        def poll_events(self):
            raise RuntimeError("notification token unavailable")

    result, _ = _run_once_with_provider(tmp_path, _Exploding(lambda: []), dry_run=True)
    assert result["alert_count"] == 0
    assert "notification token unavailable" in result["poll_error"]
    json.dumps(result)


# ── routed in: the configure helpers need a failure signal ─────────────────


def test_configure_webhook_reports_success(tmp_path: Path):
    path = tmp_path / "daemon.json"
    result = configure_webhook("http://localhost:9/hook", str(path))
    assert result["ok"] is True
    assert result["delivery"]["webhook_url"] == "http://localhost:9/hook"
    assert json.loads(path.read_text())["delivery"]["webhook_url"] == (
        "http://localhost:9/hook"
    )


def test_configure_webhook_reports_failure_instead_of_raising(tmp_path: Path, monkeypatch):
    path = tmp_path / "daemon.json"

    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("tahuti.daemon.save_daemon_config", _boom)
    result = configure_webhook("http://localhost:9/hook", str(path))

    assert result["ok"] is False
    assert "read-only file system" in result["error"]
    assert not path.exists()


def test_configure_channel_send_reports_success_and_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "daemon.json"
    result = configure_channel_send("qq", "group-1", str(path))
    assert result["ok"] is True
    assert result["delivery"]["mode"] == "channel_send"

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("tahuti.daemon.save_daemon_config", _boom)
    failed = configure_channel_send("qq", "group-1", str(path))
    assert failed["ok"] is False
    assert "disk full" in failed["error"]

