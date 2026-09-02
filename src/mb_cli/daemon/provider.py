"""Pluggable notification providers for the daemon."""

from __future__ import annotations

import abc
import logging
import re
from typing import Any
from bs4 import BeautifulSoup
import requests

from ..client import ManageBacClient
from ..notifications import MNNHubClient, hub_for_domain
from .events import MBEvent

log = logging.getLogger(__name__)


class AbstractNotificationProvider(abc.ABC):
    """Abstract base class for notification source providers."""

    @abc.abstractmethod
    def start(self) -> None:
        """Initialize provider connections or background resources."""
        ...

    @abc.abstractmethod
    def stop(self) -> None:
        """Tear down provider resources."""
        ...

    @abc.abstractmethod
    def poll_events(self) -> list[MBEvent]:
        """Fetch and return newly available normalized events."""
        ...

    @abc.abstractmethod
    def refresh_auth(self) -> bool:
        """Refresh authentication tokens or sessions."""
        ...


class MNNHubProvider(AbstractNotificationProvider):
    """Notification provider using ManageBac Notification Network (MNN Hub) REST API."""

    def __init__(self, client: ManageBacClient):
        self.client = client
        self.hub: MNNHubClient | None = None
        self.hub_endpoint: str | None = None
        self.token: str | None = None

    def _ensure_hub(self, force_refresh: bool = False) -> MNNHubClient:
        if self.hub is None or force_refresh:
            try:
                endpoint, token = self.client.get_notification_token(bypass_cache=True)
                if not endpoint:
                    endpoint = hub_for_domain(self.client.domain)
                self.hub_endpoint = endpoint
                self.token = token
                self.hub = MNNHubClient(
                    endpoint, token, verify=self.client.session.verify
                )
            except Exception as exc:
                log.error("Failed to get notification token: %s", exc)
                raise
        return self.hub

    def start(self) -> None:
        self._ensure_hub()

    def stop(self) -> None:
        self.hub = None

    def refresh_auth(self) -> bool:
        """Force refresh MNN Hub JWT token."""
        try:
            self._ensure_hub(force_refresh=True)
            return True
        except Exception as exc:
            log.error("Error refreshing notification auth: %s", exc)
            return False

    def get_stats(self) -> dict[str, Any]:
        """Fetch unread message stats with auto-retry on token expiration."""
        hub = self._ensure_hub()
        try:
            return hub.stats()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                log.warning("MNN Hub stats returned %d — refreshing token...", exc.response.status_code)
                if self.refresh_auth() and self.hub:
                    return self.hub.stats()
            raise

    def fetch_raw_notifications(self, per_page: int = 20) -> list[dict[str, Any]]:
        """Fetch recent notifications with auto-retry on token expiration."""
        hub = self._ensure_hub()
        try:
            res = hub.list(page=1, per_page=per_page, filter_="all")
            return res.get("items", [])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                log.warning("MNN Hub list returned %d — refreshing token...", exc.response.status_code)
                if self.refresh_auth() and self.hub:
                    res = self.hub.list(page=1, per_page=per_page, filter_="all")
                    return res.get("items", [])
            raise

    def poll_events(self) -> list[MBEvent]:
        """Fetch recent notifications and normalize them into MBEvents."""
        raw_items = self.fetch_raw_notifications(per_page=20)
        events: list[MBEvent] = []
        for item in raw_items:
            event = self.normalize_notification(item)
            if event:
                events.append(event)
        return events

    def normalize_notification(self, item: dict[str, Any]) -> MBEvent:
        """Convert a raw MNN Hub notification item into a standardized MBEvent."""
        raw_event = item.get("event_name") or "notification"
        notif_id = item.get("id")
        created_at = item.get("created_at")
        title = item.get("title") or "ManageBac Notification"
        body_html = item.get("body") or ""
        body_preview = item.get("body_preview") or ""
        sender = item.get("sender") or {}
        origin = item.get("origin") or {}

        # Parse task / class details from HTML if present
        task_id: int | None = None
        class_id: int | None = None
        task_url: str | None = None
        when_str: str | None = None

        if body_html:
            soup = BeautifulSoup(body_html, "html.parser")
            # Look for /student/classes/{class_id}/core_tasks/{task_id}
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", href)
                if m:
                    class_id = int(m.group(1))
                    task_id = int(m.group(2))
                    task_url = href
                    break
                # Fallback to calendar link if task link missing
                m_cal = re.search(r"/student/classes/(\d+)/calendar", href)
                if m_cal and not class_id:
                    class_id = int(m_cal.group(1))

            # Look for "When: <date string>"
            when_p = soup.find(lambda el: el.name == "p" and "When:" in el.get_text())
            if when_p:
                when_text = when_p.get_text(strip=True)
                when_str = re.sub(r"^When:\s*", "", when_text)

        event_type = self._map_event_type(raw_event)
        event_data: dict[str, Any] = {
            "notification_id": notif_id,
            "raw_event_name": raw_event,
            "title": title,
            "created_at": created_at,
            "body_preview": body_preview,
            "sender": sender,
            "origin": origin,
        }
        if task_id is not None:
            event_data["task_id"] = task_id
        if class_id is not None:
            event_data["class_id"] = class_id
        if task_url:
            event_data["url"] = task_url
        if when_str:
            event_data["due_date"] = when_str

        return MBEvent(
            event=event_type,
            event_id=f"notif_{notif_id}",
            timestamp=created_at or "",
            data=event_data,
        )

    @staticmethod
    def _map_event_type(raw_event: str) -> str:
        mapping = {
            "task_created": "task_created",
            "task_updated": "task_updated",
            "assignment_graded": "assignment_graded",
            "grade_posted": "assignment_graded",
            "announcement_created": "announcement_created",
            "message_created": "announcement_created",
        }
        return mapping.get(raw_event, "notification")


class MobilePushProvider(AbstractNotificationProvider):
    """Pluggable provider for mobile app push notifications (iOS APNs / Android push bridge)."""

    def __init__(self):
        self._is_running = False
        self._queued_events: list[MBEvent] = []

    def start(self) -> None:
        self._is_running = True

    def stop(self) -> None:
        self._is_running = False

    def refresh_auth(self) -> bool:
        return True

    def push_event(self, event: MBEvent) -> None:
        """Enqueue an event received from an external mobile push bridge or Stream proxy."""
        self._queued_events.append(event)

    def poll_events(self) -> list[MBEvent]:
        events = list(self._queued_events)
        self._queued_events.clear()
        return events
