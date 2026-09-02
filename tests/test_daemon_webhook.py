"""Tests for webhook dispatcher."""

import json
import pytest
import requests_mock
from mb_cli.daemon.events import MBEvent, WebhookConfig
from mb_cli.daemon.webhook import WebhookDispatcher


def test_webhook_dispatch_success():
    wh_url = "http://localhost:8888/webhook"
    wh = WebhookConfig(url=wh_url, secret="test-secret")
    dispatcher = WebhookDispatcher(webhooks=[wh])

    event = MBEvent(
        event="deadline_approaching",
        data={"task_id": "999", "title": "Final Project"},
    )

    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        results = dispatcher.dispatch(event)
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["status_code"] == 200

        # Verify signature header
        req = m.last_request
        assert req.headers.get("X-MB-Event") == "deadline_approaching"
        assert req.headers.get("X-MB-Signature", "").startswith("sha256=")


def test_webhook_test_ping():
    wh_url = "http://localhost:8888/test-hook"
    dispatcher = WebhookDispatcher()

    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        res = dispatcher.test_ping(wh_url)
        assert res["success"] is True
        assert res["status_code"] == 200
