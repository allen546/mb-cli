"""Tests for daemon event schemas, standard MBEvent payloads, and configurations."""

import json
import pytest
from mb_cli.daemon.events import (
    DEFAULT_REMINDER_THRESHOLDS,
    STANDARD_TASK_FIELDS,
    DaemonConfig,
    MBEvent,
    ReminderThreshold,
    StealthConfig,
    WebhookConfig,
    standardize_task_payload,
)


def test_standard_mbevent_serialization():
    """Verify that MBEvent(event="task_graded", data={...}) serializes all standard fields cleanly."""
    data_payload = {
        "task_id": 12345,
        "class_id": 67890,
        "class_name": "AP Physics 1",
        "title": "Lab Practical",
        "due_date": "2026-09-20 12:00:00",
        "due_iso": "2026-09-20T12:00:00+08:00",
        "has_submit_button": False,
        "category": "Quiz",
        "status": "not-submitted",
        "grade_letter": "A",
        "grade_score": "95 / 100",
        "url": "https://example.managebac.cn/student/classes/67890/core_tasks/12345",
    }
    event = MBEvent(
        event="task_graded",
        data=data_payload,
    )
    d = event.to_dict()

    # Verify envelope fields
    assert d["event"] == "task_graded"
    assert d["version"] == "1.0"
    assert "timestamp" in d
    assert "event_id" in d
    assert d["event_id"].startswith("evt_")
    assert "data" in d

    # Verify all standard fields in data are serialized cleanly
    for field_name in STANDARD_TASK_FIELDS:
        assert field_name in d["data"], f"Missing field: {field_name}"

    assert d["data"]["task_id"] == 12345
    assert d["data"]["class_id"] == 67890
    assert d["data"]["class_name"] == "AP Physics 1"
    assert d["data"]["title"] == "Lab Practical"
    assert d["data"]["due_date"] == "2026-09-20 12:00:00"
    assert d["data"]["due_iso"] == "2026-09-20T12:00:00+08:00"
    assert d["data"]["has_submit_button"] is False
    assert d["data"]["category"] == "Quiz"
    assert d["data"]["status"] == "not-submitted"
    assert d["data"]["grade_letter"] == "A"
    assert d["data"]["grade_score"] == "95 / 100"
    assert d["data"]["url"] == "https://example.managebac.cn/student/classes/67890/core_tasks/12345"

    # Verify to_json() behaves properly
    json_str = event.to_json()
    loaded = json.loads(json_str)
    assert loaded == d
    assert loaded["event"] == "task_graded"
    assert loaded["data"]["task_id"] == 12345

    # Verify indented to_json()
    json_indented = event.to_json(indent=2)
    assert "\n" in json_indented
    assert json.loads(json_indented) == d


def test_standardize_task_payload():
    """Verify standardization of sparse or raw task data into standard schema."""
    raw = {
        "id": "27535638",
        "class_id": "11511739",
        "title": "New Task: Vocab Quiz 2",
        "due_date": "2026-09-15 10:00:00",
        "has_submit_button": 0,
        "labels": ["Quiz", "Formative"],
        "link": "https://example.managebac.cn/student/classes/11511739/core_tasks/27535638",
    }
    std = standardize_task_payload(raw)

    assert std["task_id"] == 27535638
    assert std["class_id"] == 11511739
    assert std["class_name"] is None
    assert std["title"] == "Vocab Quiz 2"
    assert std["due_date"] == "2026-09-15 10:00:00"
    assert std["due_iso"] is not None
    assert "2026-09-15" in std["due_iso"]
    assert std["has_submit_button"] is False
    assert std["category"] == "Quiz"
    assert std["status"] is None
    assert std["grade_letter"] is None
    assert std["grade_score"] is None
    assert std["url"] == "https://example.managebac.cn/student/classes/11511739/core_tasks/27535638"


def test_mbevent_factories():
    """Verify MBEvent factory methods from_task and create."""
    task = {
        "task_id": "999",
        "class_id": "888",
        "title": "Essay",
        "due_date": "2026-10-01 12:00:00",
        "has_submit_button": True,
    }
    evt1 = MBEvent.from_task("task_created", task, custom_field="extra_value")
    assert evt1.event == "task_created"
    assert evt1.data["task_id"] == 999
    assert evt1.data["has_submit_button"] is True
    assert evt1.data["custom_field"] == "extra_value"
    assert evt1.data["due_iso"] is not None

    evt2 = MBEvent.create("task_updated", task, standardize=True)
    assert evt2.event == "task_updated"
    assert evt2.data["task_id"] == 999
    assert evt2.data["category"] is None
    assert evt2.data["status"] is None


def test_mbevent_validation():
    """Verify validation of event envelope and standard task fields."""
    valid_data = {
        "task_id": 1,
        "class_id": 2,
        "class_name": "Math",
        "title": "Homework",
        "due_date": "2026-09-15 10:00:00",
        "due_iso": "2026-09-15T10:00:00",
        "has_submit_button": True,
        "category": "Homework",
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "url": "https://example.com/task/1",
    }
    event = MBEvent(event="task_created", data=valid_data)
    assert event.validate() is True

    # Missing standard field in task event
    invalid_data = dict(valid_data)
    del invalid_data["due_iso"]
    invalid_event = MBEvent(event="task_created", data=invalid_data)
    assert invalid_event.validate(strict=False) is False
    with pytest.raises(ValueError, match="Missing standard task field: 'due_iso'"):
        invalid_event.validate(strict=True)

    # Non-boolean has_submit_button
    bad_bool_data = dict(valid_data)
    bad_bool_data["has_submit_button"] = "yes"  # type: ignore
    bad_bool_event = MBEvent(event="task_created", data=bad_bool_data)
    assert bad_bool_event.validate(strict=False) is False
    with pytest.raises(ValueError, match="Field 'has_submit_button' must be a boolean"):
        bad_bool_event.validate(strict=True)

    # Non-task event (e.g. file_uploaded) does not require task fields
    file_event = MBEvent(event="file_uploaded", data={"filename": "notes.pdf"})
    assert file_event.validate() is True

    # Empty event name
    empty_event = MBEvent(event="", data={})
    assert empty_event.validate(strict=False) is False
    with pytest.raises(ValueError, match="Field 'event' must be a non-empty string"):
        empty_event.validate(strict=True)


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
