"""Event schemas, data models, and configurations for the daemon and webhooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any
import uuid

STANDARD_TASK_FIELDS: tuple[str, ...] = (
    "task_id",
    "class_id",
    "class_name",
    "title",
    "due_date",
    "due_iso",
    "has_submit_button",
    "category",
    "status",
    "grade_letter",
    "grade_score",
    "url",
)

TASK_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_created",
        "task_updated",
        "task_graded",
        "deadline_approaching",
    }
)


def standardize_task_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure standard task fields are consistently present and formatted.

    Standard fields:
        task_id: int | str | None
        class_id: int | str | None
        class_name: str | None
        title: str
        due_date: str | None
        due_iso: str | None (ISO-8601 string)
        has_submit_button: bool
        category: str | None
        status: str | None
        grade_letter: str | None
        grade_score: str | None
        url: str | None
    """
    payload = dict(data)

    # 1. task_id
    raw_tid = payload.get("task_id")
    if raw_tid is None:
        raw_tid = payload.get("id")
    if raw_tid is not None and str(raw_tid).isdigit():
        payload["task_id"] = int(raw_tid)
    else:
        payload["task_id"] = raw_tid

    # 2. class_id
    raw_cid = payload.get("class_id")
    if raw_cid is not None and str(raw_cid).isdigit():
        payload["class_id"] = int(raw_cid)
    else:
        payload["class_id"] = raw_cid

    # 3. class_name
    cls_name = payload.get("class_name")
    payload["class_name"] = str(cls_name) if cls_name is not None else None

    # 4. title
    title = payload.get("title") or payload.get("task_title") or ""
    if isinstance(title, str):
        cleaned_title = re.sub(
            r"^(?:New\s+Task|Updated\s+Task|Task):\s*", "", title, flags=re.IGNORECASE
        ).strip()
        payload["title"] = cleaned_title or title
    else:
        payload["title"] = str(title)

    # 5. due_date
    due_date = payload.get("due_date")
    payload["due_date"] = str(due_date) if due_date is not None else None

    # 6. due_iso
    due_iso = payload.get("due_iso")
    if not due_iso and due_date:
        try:
            from ..client import parse_due_date

            dt = parse_due_date(str(due_date))
            if dt is not None:
                due_iso = dt.isoformat()
        except Exception:
            due_iso = None
    payload["due_iso"] = str(due_iso) if due_iso else None

    # 7. has_submit_button
    payload["has_submit_button"] = bool(payload.get("has_submit_button", False))

    # 8. category
    cat = payload.get("category")
    if not cat:
        labels = payload.get("labels")
        if isinstance(labels, list) and labels:
            cat = labels[0]
    payload["category"] = str(cat) if cat is not None else None

    # 9. status
    status = payload.get("status")
    payload["status"] = str(status) if status is not None else None

    # 10. grade_letter
    gl = payload.get("grade_letter")
    payload["grade_letter"] = str(gl) if gl is not None else None

    # 11. grade_score
    gs = payload.get("grade_score")
    payload["grade_score"] = str(gs) if gs is not None else None

    # 12. url
    url = payload.get("url") or payload.get("link")
    payload["url"] = str(url) if url is not None else None

    return payload


@dataclass
class MBEvent:
    """Standardized event envelope dispatched to webhooks."""

    event: str
    data: dict[str, Any]
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event": self.event,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    def to_json(self, indent: int | None = None) -> str:
        if indent is None:
            return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def validate(self, strict: bool = True) -> bool:
        """Validate event envelope and task schema compliance."""
        errors: list[str] = []
        if not self.event or not isinstance(self.event, str):
            errors.append("Field 'event' must be a non-empty string.")
        if not isinstance(self.data, dict):
            errors.append("Field 'data' must be a dictionary.")
        if not self.event_id or not isinstance(self.event_id, str):
            errors.append("Field 'event_id' must be a non-empty string.")
        if not self.timestamp or not isinstance(self.timestamp, str):
            errors.append("Field 'timestamp' must be a non-empty string.")
        if not self.version or not isinstance(self.version, str):
            errors.append("Field 'version' must be a non-empty string.")

        if self.event in TASK_EVENT_TYPES and isinstance(self.data, dict):
            for f in STANDARD_TASK_FIELDS:
                if f not in self.data:
                    errors.append(f"Missing standard task field: '{f}'")
            if "has_submit_button" in self.data and not isinstance(
                self.data["has_submit_button"], bool
            ):
                errors.append("Field 'has_submit_button' must be a boolean.")

        if errors:
            if strict:
                raise ValueError("; ".join(errors))
            return False
        return True

    @classmethod
    def from_task(
        cls,
        event: str,
        task: dict[str, Any],
        event_id: str | None = None,
        timestamp: str | None = None,
        version: str = "1.0",
        **extra_fields: Any,
    ) -> MBEvent:
        """Factory constructing a standardized MBEvent from task data."""
        payload = standardize_task_payload(task)
        if extra_fields:
            payload.update(extra_fields)
        kwargs: dict[str, Any] = {
            "event": event,
            "data": payload,
            "version": version,
        }
        if event_id is not None:
            kwargs["event_id"] = event_id
        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        return cls(**kwargs)

    @classmethod
    def create(
        cls,
        event: str,
        data: dict[str, Any],
        event_id: str | None = None,
        timestamp: str | None = None,
        version: str = "1.0",
        standardize: bool = False,
    ) -> MBEvent:
        """Factory creating an MBEvent envelope with optional payload standardization."""
        payload = standardize_task_payload(data) if standardize else data
        kwargs: dict[str, Any] = {
            "event": event,
            "data": payload,
            "version": version,
        }
        if event_id is not None:
            kwargs["event_id"] = event_id
        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        return cls(**kwargs)


@dataclass
class ReminderThreshold:
    """Threshold for approaching deadline countdown alerts."""

    threshold_minutes: int
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReminderThreshold:
        return cls(
            threshold_minutes=int(data.get("threshold_minutes", 60)),
            name=str(data.get("name", "1h")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_minutes": self.threshold_minutes,
            "name": self.name,
        }


DEFAULT_REMINDER_THRESHOLDS = [
    ReminderThreshold(threshold_minutes=1440, name="24h"),
    ReminderThreshold(threshold_minutes=360, name="6h"),
    ReminderThreshold(threshold_minutes=60, name="1h"),
    ReminderThreshold(threshold_minutes=15, name="15m"),
]


@dataclass
class WebhookConfig:
    """Configuration for a webhook destination."""

    url: str
    secret: str | None = None
    events: list[str] = field(default_factory=lambda: ["*"])
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookConfig:
        return cls(
            url=str(data.get("url", "")),
            secret=data.get("secret"),
            events=list(data.get("events", ["*"])),
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "secret": self.secret,
            "events": self.events,
            "enabled": self.enabled,
        }

    def matches_event(self, event_type: str) -> bool:
        """Check if this webhook is subscribed to the given event type."""
        if not self.enabled:
            return False
        if "*" in self.events:
            return True
        return event_type in self.events


@dataclass
class StealthConfig:
    """Configuration for human-like stealth page browsing."""

    enabled: bool = True
    fetch_parent_context: bool = True
    min_jitter_seconds: float = 1.0
    max_jitter_seconds: float = 3.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StealthConfig:
        return cls(
            enabled=bool(data.get("enabled", True)),
            fetch_parent_context=bool(data.get("fetch_parent_context", True)),
            min_jitter_seconds=float(data.get("min_jitter_seconds", 1.0)),
            max_jitter_seconds=float(data.get("max_jitter_seconds", 3.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fetch_parent_context": self.fetch_parent_context,
            "min_jitter_seconds": self.min_jitter_seconds,
            "max_jitter_seconds": self.max_jitter_seconds,
        }


@dataclass
class DaemonConfig:
    """Full daemon configuration."""

    enabled: bool = True
    provider: str = "mnn_hub"
    poll_interval_seconds: int = 30
    poll_jitter_seconds: int = 5
    full_sync_interval_minutes: int = 15
    reminders: list[ReminderThreshold] = field(
        default_factory=lambda: list(DEFAULT_REMINDER_THRESHOLDS)
    )
    webhooks: list[WebhookConfig] = field(default_factory=list)
    stealth: StealthConfig = field(default_factory=StealthConfig)
    verify_tls: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DaemonConfig:
        reminders_data = data.get("reminders")
        if reminders_data is not None:
            reminders = [
                ReminderThreshold.from_dict(r) if isinstance(r, dict) else r
                for r in reminders_data
            ]
        else:
            reminders = list(DEFAULT_REMINDER_THRESHOLDS)

        webhooks_data = data.get("webhooks", [])
        webhooks = [
            WebhookConfig.from_dict(w) if isinstance(w, dict) else w
            for w in webhooks_data
        ]

        stealth_data = data.get("stealth", {})
        stealth = (
            StealthConfig.from_dict(stealth_data)
            if isinstance(stealth_data, dict)
            else StealthConfig()
        )

        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=str(data.get("provider", "mnn_hub")),
            poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
            poll_jitter_seconds=int(data.get("poll_jitter_seconds", 5)),
            full_sync_interval_minutes=int(
                data.get("full_sync_interval_minutes", 15)
            ),
            reminders=reminders,
            webhooks=webhooks,
            stealth=stealth,
            verify_tls=bool(data.get("verify_tls", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_jitter_seconds": self.poll_jitter_seconds,
            "full_sync_interval_minutes": self.full_sync_interval_minutes,
            "reminders": [r.to_dict() for r in self.reminders],
            "webhooks": [w.to_dict() for w in self.webhooks],
            "stealth": self.stealth.to_dict(),
            "verify_tls": self.verify_tls,
        }
