"""Pluggable notification providers for the daemon."""

from __future__ import annotations

import abc
from collections.abc import Callable
import logging
import re
from typing import Any
from bs4 import BeautifulSoup
import requests

from ..client import ManageBacClient, parse_task_url
from ..notifications import MNNHubClient
from .events import MBEvent

log = logging.getLogger(__name__)


def _coerce_notification_id(raw: Any) -> int | None:
    """Return ``raw`` as an int notification id, or ``None`` if it is not one.

    ``DaemonService.run_check_cycle`` calls ``int()`` on ``data["notification_id"]``
    at three places — the processed-set lookup (service.py:217), the suppression
    marker (:308) and the post-dispatch marker (:356) — guarded only by
    ``if notif_id``, inside a single ``try`` that wraps the whole event loop. So a
    hub id that is truthy but not an int (``"abc-123"``, ``12.5``, a dict) raises
    ValueError there and aborts the cycle, silently dropping every notification
    queued behind it. Normalizing here means the loop only ever sees an int or
    ``None``, and ``None`` is already the case it guards for.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    # `str.isdigit()` is True for characters `int()` refuses — superscripts
    # ('²'), subscripts ('₁'), and other Unicode digits — so it would raise the
    # very ValueError this function exists to prevent, aborting the poll cycle
    # and dropping every notification queued behind it. `isdecimal()` is also
    # wider than needed, so pin it to ASCII digits.
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        return int(raw.strip())
    return None


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

    def __init__(
        self,
        client: ManageBacClient,
        auth_refresh_fn: Callable[[], bool] | None = None,
    ):
        self.client = client
        self.auth_refresh_fn = auth_refresh_fn
        self.hub: MNNHubClient | None = None
        self.hub_endpoint: str | None = None
        self.token: str | None = None

    def _ensure_hub(self, force_refresh: bool = False) -> MNNHubClient:
        if self.hub is not None and not force_refresh:
            return self.hub
        try:
            endpoint, token = self._acquire_token()
            # `data-mnn-hub-endpoint` is scraped HTML and the token goes out as
            # `Authorization: Bearer <jwt>`, so the raw value cannot pick the
            # host it is sent to. See ManageBacClient._validated_hub_endpoint.
            self.hub_endpoint = self.client._validated_hub_endpoint(endpoint)
            self.token = token
            self.hub = MNNHubClient(
                self.hub_endpoint, token, verify=self.client.session.verify
            )
        except Exception as exc:
            # The only observable effect of this handler is the log line; the
            # bare re-raise is what every path did before.
            log.error("Failed to get notification token: %s", exc)
            raise
        return self.hub

    def _acquire_token(self) -> tuple[str, str]:
        """Fetch a hub (endpoint, JWT), relogging in once if the session expired."""
        try:
            return self.client.get_notification_token(bypass_cache=True)
        except Exception as exc:
            expired = "Session expired" in str(exc) or "login" in str(exc).lower()
            if not (expired and self.auth_refresh_fn):
                raise
            log.info(
                "Session expired while acquiring notification token"
                " — attempting auto-relogin..."
            )
            if not self.auth_refresh_fn():
                raise
            return self.client.get_notification_token(bypass_cache=True)

    def start(self) -> None:
        self._ensure_hub()

    def stop(self) -> None:
        self.hub = None

    def refresh_auth(self) -> bool:
        """Force refresh MNN Hub JWT token with session relogin fallback."""
        try:
            if self.auth_refresh_fn:
                log.info("Refreshing ManageBac web session via auth callback...")
                self.auth_refresh_fn()
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
        notif_id = _coerce_notification_id(item.get("id"))
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
                cid, tid = parse_task_url(href)
                if cid and tid:
                    class_id = int(cid)
                    task_id = int(tid)
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

        event_type = self._map_event_type(raw_event, title)
        
        # Clean task title if prefixed with "New Task: " or "Updated Task: "
        clean_task_title = re.sub(r"^(?:New\s+Task|Updated\s+Task|Task):\s*", "", title, flags=re.I).strip()
        class_name = (origin.get("name") if origin else "") or ""

        raw_class_clean = re.sub(r"\(.*?(?:\)|$)", "", class_name).strip().lower().rstrip(". ")
        clean_task_clean = re.sub(r"\(.*?(?:\)|$)", "", clean_task_title).strip().lower()
        is_class_match = (
            (class_name and clean_task_title.lower() == class_name.lower())
            or (len(raw_class_clean) >= 5 and raw_class_clean in clean_task_clean)
            or (len(raw_class_clean) >= 5 and clean_task_clean in raw_class_clean)
        )

        # Extract specific task title from body if title is generic or equals class name
        is_generic = (
            not clean_task_title
            or clean_task_title.lower() in ("updated task", "new task", "task", "managebac notification", "notification")
            or is_class_match
        )
        if is_generic:
            extracted_title = None
            if body_html:
                m_body = re.search(
                    r"(?:added a new|updated the|created a|added the)\s+Task\s+<strong[^>]*>(.*?)</strong>",
                    body_html,
                    re.IGNORECASE,
                )
                if not m_body:
                    m_body = re.search(r"Task\s+<strong[^>]*>(.*?)</strong>", body_html, re.IGNORECASE)
                if m_body:
                    extracted_title = BeautifulSoup(m_body.group(1), "html.parser").get_text(strip=True)
            if not extracted_title and body_preview:
                m_prev = re.search(
                    r"(?:added a new|updated the|created a|added the)\s+Task\s+(.*?)\s+in\s+",
                    body_preview,
                    re.IGNORECASE,
                )
                if not m_prev:
                    m_prev = re.search(r"Task\s+(.*?)\s+in\s+", body_preview, re.IGNORECASE)
                if m_prev:
                    extracted_title = m_prev.group(1).strip()

            if extracted_title and (not class_name or extracted_title.lower() != class_name.lower()):
                clean_task_title = extracted_title

        event_data: dict[str, Any] = {
            "notification_id": notif_id,
            "raw_event_name": raw_event,
            "title": title,
            "task_title": clean_task_title or title,
            "class_name": class_name,
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

        # A missing or unusable id gets MBEvent's uuid default rather than the
        # literal string "notif_None", which every id-less notification would
        # share and which would be shipped to webhooks as an event_id.
        event_kwargs: dict[str, Any] = {}
        if notif_id is not None:
            event_kwargs["event_id"] = f"notif_{notif_id}"

        return MBEvent(
            event=event_type,
            timestamp=created_at or "",
            data=event_data,
            **event_kwargs,
        )

    @staticmethod
    def _map_event_type(raw_event: str, title: str = "") -> str:
        mapping = {
            "task_created": "task_created",
            "new_task": "task_created",
            "task_updated": "task_updated",
            "updated_task": "task_updated",
            "assignment_graded": "assignment_graded",
            "grade_posted": "assignment_graded",
            "new_file_uploaded": "file_uploaded",
            "file_uploaded": "file_uploaded",
            "announcement_created": "announcement_created",
            "new_announcement": "announcement_created",
            "message_created": "announcement_created",
        }
        mapped = mapping.get(raw_event)
        if mapped:
            return mapped

        # Fallback based on title keywords
        t_low = title.lower()
        if "new task" in t_low or "task added" in t_low or "task created" in t_low:
            return "task_created"
        if "updated task" in t_low or "task updated" in t_low:
            return "task_updated"
        if "file uploaded" in t_low or "new file" in t_low:
            return "file_uploaded"
        if "graded" in t_low or "grade" in t_low:
            return "assignment_graded"
        if "announcement" in t_low:
            return "announcement_created"

        return "notification"


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
