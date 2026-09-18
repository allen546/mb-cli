"""Complete end-to-end integration test for the ManageBac notification daemon and webhook system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
import pytest

from mb_cli.__main__ import main
from mb_cli.daemon.events import (
    DaemonConfig,
    MBEvent,
    ReminderThreshold,
    StealthConfig,
    WebhookConfig,
)
from mb_cli.daemon.provider import MNNHubProvider
from mb_cli.daemon.scheduler import DDLScheduler
from mb_cli.daemon.service import DaemonService
from mb_cli.daemon.state import DaemonStateManager
from mb_cli.daemon.system import ServiceManager
from mb_cli.daemon.webhook import WebhookDispatcher


class WebhookRecordingServer:
    """Threaded HTTP server to capture dispatched webhook POST requests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.received_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    payload = {}

                record = {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body_bytes": body,
                    "payload": payload,
                }
                outer.received_requests.append(record)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, format, *args):
                pass  # Silence stdout logging

        self.server = HTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}/webhook"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_e2e_webhook_ping_and_signature(tmp_path: Path):
    """E2E Test 1: CLI test-webhook sends signed payload to live HTTP receiver."""
    server = WebhookRecordingServer()
    server.start()
    secret = "super-secret-hmac-key"

    try:
        # Run test-webhook via WebhookDispatcher
        dispatcher = WebhookDispatcher()
        res = dispatcher.test_ping(server.url, secret=secret)
        assert res["success"] is True
        assert res["status_code"] == 200

        # Verify server captured request
        assert len(server.received_requests) == 1
        req = server.received_requests[0]
        assert req["headers"].get("X-MB-Event") == "test_ping"

        # Verify HMAC signature
        signature_header = req["headers"].get("X-MB-Signature")
        assert signature_header is not None
        expected_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"), req["body_bytes"], hashlib.sha256
        ).hexdigest()
        assert signature_header == expected_sig

        # Verify envelope structure
        payload = req["payload"]
        assert payload["version"] == "1.0"
        assert payload["event"] == "test_ping"
        assert "evt_" in payload["event_id"]
        assert "ManageBac Webhook Test Ping" in payload["data"]["message"]

    finally:
        server.stop()


def test_e2e_full_daemon_check_cycle_pipeline(tmp_path: Path):
    """E2E Test 2: Full pipeline from notification polling, stealth enrichment, deadline scheduling to webhook delivery and state persistence."""
    server = WebhookRecordingServer()
    server.start()
    secret = "e2e-secret-key"

    try:
        state_file = tmp_path / "daemon_state.json"
        state_manager = DaemonStateManager(state_file)

        # 1. Setup Mock Client with realistic HTML responses
        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.base = "https://school.managebac.cn"
        mock_client.get_notification_token.return_value = (
            "https://mnn-hub.prod.faria.cn",
            "fake_jwt_token",
        )

        now = datetime.now(timezone.utc)
        due_dt = now + timedelta(minutes=40)  # Due in 40m -> should trigger 1h milestone

        # Mock upcoming tasks
        mock_client.get_tasks_by_view.return_value = [
            {
                "id": "1000099",
                "title": "NAME LIST",
                "class_id": "1000012",
                "class_name": "AP Physics 1",
                # isoformat() keeps the offset: parse_due_date returns aware datetimes,
                # and a strftime'd string carrying no offset would be read as the host's
                # local time rather than this test's UTC frame.
                "due_date": due_dt.isoformat(),
                "status": "not-submitted",
            },
            {
                "id": "99999999",
                "title": "Already Completed Math Task",
                "class_id": "1000012",
                "class_name": "Math HL",
                "due_date": due_dt.isoformat(),
                "status": "submitted",  # Should be silenced
            },
        ]

        # Mock stealth crawler response for newly created task
        sample_task_html = """
        <html>
          <body>
            <h3 class="title">New History Essay</h3>
            <a href="/student/classes/1000012">AP US History</a>
            <span class="badge label-status">Not Submitted</span>
            <p>Due: September 15, 2026 at 23:59</p>
            <a href="/student/classes/1000012/core_tasks/1000300/dropbox">Submit</a>
          </body>
        </html>
        """
        mock_client._get.return_value = BeautifulSoup(sample_task_html, "html.parser")

        # Mock notification provider returning one new notification
        raw_notification = {
            "id": 100200,
            "title": "New assignment created",
            "event_name": "task_created",
            "created_at": "2026-09-10T08:25:00.000Z",
            "body": '<p>Teacher posted <a href="https://school.managebac.cn/student/classes/1000012/core_tasks/1000300">History Essay</a></p><p>When: September 15, 2026 at 23:59</p>',
            "body_preview": "New assignment posted by Mr. Smith",
            "sender": {"name": "Mr. Smith"},
            "origin": {"name": "AP US History"},
        }

        mock_provider = MagicMock()
        hub_prov = MNNHubProvider(mock_client)
        normalized_evt = hub_prov.normalize_notification(raw_notification)
        mock_provider.poll_events.return_value = [normalized_evt]

        # 2. Configure Daemon
        config = DaemonConfig(
            poll_interval_seconds=30,
            webhooks=[
                WebhookConfig(
                    url=server.url,
                    secret=secret,
                    events=["*"],
                    enabled=True,
                )
            ],
            reminders=[
                ReminderThreshold(threshold_minutes=1440, name="24h"),
                ReminderThreshold(threshold_minutes=60, name="1h"),
                ReminderThreshold(threshold_minutes=15, name="15m"),
            ],
            stealth=StealthConfig(enabled=False),  # disable delay for tests
        )

        service = DaemonService(
            client=mock_client,
            config=config,
            state_manager=state_manager,
            provider=mock_provider,
        )

        # Initial sync
        with patch.object(service, "_suppress_past_milestones"):
            synced = service.sync_upcoming_tasks()
        assert synced == 2
        assert state_manager.get_task(1000099) is not None

        # 3. Run check cycle at `now`
        # Patch DDLScheduler to evaluate with `now`
        with patch.object(service.scheduler, "evaluate_deadlines", side_effect=lambda **kw: DDLScheduler(state_manager, config.reminders).evaluate_deadlines(now=now, auto_mark=False)):
            cycle_result = service.run_check_cycle()

        assert cycle_result["new_notifications"] == 1
        assert cycle_result["reminders_dispatched"] >= 1

        # Verify state manager persisted records
        assert state_manager.is_notification_processed(100200)
        assert state_manager.is_reminder_dispatched("1000099", "1h")
        # Submitted task should NOT be dispatched
        assert not state_manager.is_reminder_dispatched("99999999", "1h")

        # Verify webhooks received
        events_received = [r["payload"]["event"] for r in server.received_requests]
        assert "task_created" in events_received
        assert "deadline_approaching" in events_received

        # Verify stealth enrichment was attached
        task_created_req = next(r for r in server.received_requests if r["payload"]["event"] == "task_created")
        assert "enriched_task" in task_created_req["payload"]["data"]
        enriched = task_created_req["payload"]["data"]["enriched_task"]
        assert enriched["title"] == "New History Essay"
        assert enriched["status"] == "not-submitted"
        assert enriched["due_date"] == "September 15, 2026 at 23:59"

        # Verify state file was saved to disk
        assert state_file.exists()
        saved_disk_data = json.loads(state_file.read_text(encoding="utf-8"))
        assert 100200 in saved_disk_data["processed_notification_ids"]
        assert any("task_1000099" in r for r in saved_disk_data["dispatched_reminders"])

        # 4. Run second cycle: verify deduplication prevents duplicate events
        server.received_requests.clear()
        mock_provider.poll_events.return_value = [normalized_evt]  # same event returned
        with patch.object(service.scheduler, "evaluate_deadlines", side_effect=lambda **kw: DDLScheduler(state_manager, config.reminders).evaluate_deadlines(now=now, auto_mark=False)):
            cycle_result_2 = service.run_check_cycle()

        assert cycle_result_2["new_notifications"] == 0
        assert cycle_result_2["reminders_dispatched"] == 0
        assert len(server.received_requests) == 0

    finally:
        server.stop()


def test_e2e_session_auto_relogin_recovery(tmp_path: Path):
    """E2E Test 3: Expired session cookie during polling triggers silent re-login without dropping loop."""
    mock_client = MagicMock()
    mock_client.domain = "managebac.cn"
    mock_client.get_notification_token.side_effect = [
        RuntimeError("Session expired or invalid — redirected to login"),
        ("https://mnn-hub.prod.faria.cn", "refreshed_jwt"),
    ]

    relogin_called = False

    def fake_relogin():
        nonlocal relogin_called
        relogin_called = True
        return True

    provider = MNNHubProvider(mock_client, auth_refresh_fn=fake_relogin)
    provider.start()

    assert relogin_called is True
    assert provider.token == "refreshed_jwt"


def test_e2e_background_process_lifecycle(tmp_path: Path):
    """E2E Test 4: Background daemon startup, status query, and graceful termination."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon.log"

    mgr = ServiceManager(pid_path=pid_file, log_path=log_file)

    # Initially not running
    status = mgr.status()
    assert status["running"] is False

    # Start a mock background process representing the tahuti daemon
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(
        cmd,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    mgr.write_pid(proc.pid)

    try:
        # Check status detects it running
        status_running = mgr.status()
        assert status_running["running"] is True
        assert status_running["pid"] == proc.pid

        # Stop daemon
        stop_res = mgr.stop_background(verify_process=False)
        assert stop_res["stopped"] is True
        assert not pid_file.exists()

        # Status now stopped
        assert mgr.status()["running"] is False

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_e2e_cli_commands_subprocess(tmp_path: Path):
    """E2E Test 5: Real subprocess CLI execution of configure-webhook and test-webhook."""
    server = WebhookRecordingServer()
    server.start()
    daemon_config_path = tmp_path / "daemon.json"

    try:
        # 1. Test configure-webhook CLI command
        cmd_cfg = [
            sys.executable,
            "-m",
            "mb_cli",
            "daemon",
            "configure-webhook",
            server.url,
            "--daemon-config",
            str(daemon_config_path),
            "--format",
            "json",
        ]
        res_cfg = subprocess.run(cmd_cfg, capture_output=True, text=True)
        assert res_cfg.returncode == 0
        cfg_out = json.loads(res_cfg.stdout)
        assert cfg_out["ok"] is True
        assert daemon_config_path.exists()

        # 2. Test test-webhook CLI command targeting configured URL
        cmd_test = [
            sys.executable,
            "-m",
            "mb_cli",
            "daemon",
            "test-webhook",
            server.url,
            "--secret",
            "cli-secret-key",
            "--format",
            "json",
        ]
        res_test = subprocess.run(cmd_test, capture_output=True, text=True)
        assert res_test.returncode == 0
        test_out = json.loads(res_test.stdout)
        assert test_out["ok"] is True
        assert test_out["data"]["success"] is True

        # Verify server captured the ping
        assert len(server.received_requests) >= 1
        last_req = server.received_requests[-1]
        assert last_req["headers"].get("X-MB-Event") == "test_ping"
        assert "X-MB-Signature" in last_req["headers"]

    finally:
        server.stop()
