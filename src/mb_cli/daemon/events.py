"""Event schemas, data models, and configurations for the daemon and webhooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any
import uuid


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
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


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
