"""Tests for daemon event schemas and configurations."""

import json
from mb_cli.daemon.events import (
    DEFAULT_REMINDER_THRESHOLDS,
    DaemonConfig,
    MBEvent,
    ReminderThreshold,
    StealthConfig,
    WebhookConfig,
)


def test_mbevent_serialization():
    evt = MBEvent(
        event="deadline_approaching",
        data={"task_id": 1234, "title": "Math HW"},
    )
    d = evt.to_dict()
    assert d["event"] == "deadline_approaching"
    assert d["data"]["task_id"] == 1234
    assert d["version"] == "1.0"
    assert evt.event_id.startswith("evt_")

    json_str = evt.to_json()
    loaded = json.loads(json_str)
    assert loaded["event"] == "deadline_approaching"


def test_reminder_threshold():
    r = ReminderThreshold(threshold_minutes=60, name="1h")
    assert r.threshold_minutes == 60
    assert r.name == "1h"
    d = r.to_dict()
    r2 = ReminderThreshold.from_dict(d)
    assert r2.threshold_minutes == 60
    assert r2.name == "1h"


def test_webhook_config_matching():
    wh_wildcard = WebhookConfig(url="https://example.com/hook", events=["*"])
    assert wh_wildcard.matches_event("task_created")
    assert wh_wildcard.matches_event("deadline_approaching")

    wh_filtered = WebhookConfig(
        url="https://example.com/hook2",
        events=["deadline_approaching", "task_created"],
    )
    assert wh_filtered.matches_event("task_created")
    assert wh_filtered.matches_event("deadline_approaching")
    assert not wh_filtered.matches_event("assignment_graded")

    wh_disabled = WebhookConfig(url="https://example.com/hook3", enabled=False)
    assert not wh_disabled.matches_event("task_created")


def test_daemon_config_serialization():
    cfg = DaemonConfig(
        provider="mnn_hub",
        poll_interval_seconds=45,
        webhooks=[WebhookConfig(url="https://example.com/wh")],
    )
    d = cfg.to_dict()
    assert d["provider"] == "mnn_hub"
    assert d["poll_interval_seconds"] == 45
    assert len(d["webhooks"]) == 1

    cfg2 = DaemonConfig.from_dict(d)
    assert cfg2.provider == "mnn_hub"
    assert cfg2.poll_interval_seconds == 45
    assert len(cfg2.webhooks) == 1
    assert cfg2.webhooks[0].url == "https://example.com/wh"
