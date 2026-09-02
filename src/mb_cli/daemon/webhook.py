"""Webhook dispatcher supporting HTTP POST with signatures and exponential retries."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
import requests

from .events import MBEvent, WebhookConfig

log = logging.getLogger(__name__)


class WebhookDispatcher:
    """Dispatches MBEvents to configured webhook endpoints."""

    def __init__(
        self,
        webhooks: list[WebhookConfig] | None = None,
        verify_tls: bool = True,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.webhooks = webhooks or []
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def _compute_signature(secret: str, payload_bytes: bytes) -> str:
        """Compute HMAC-SHA256 signature for the payload."""
        return "sha256=" + hmac.new(
            secret.encode("utf-8"), payload_bytes, hashlib.sha256
        ).hexdigest()

    def dispatch(self, event: MBEvent) -> list[dict[str, Any]]:
        """Dispatch an event to all matching enabled webhooks."""
        results: list[dict[str, Any]] = []
        payload_dict = event.to_dict()
        payload_bytes = json.dumps(
            payload_dict, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        for webhook in self.webhooks:
            if not webhook.matches_event(event.event):
                continue

            success, status_code, err_msg = self._post_with_retry(
                webhook, payload_bytes, event.event
            )
            results.append(
                {
                    "url": webhook.url,
                    "event": event.event,
                    "event_id": event.event_id,
                    "success": success,
                    "status_code": status_code,
                    "error": err_msg,
                }
            )

        return results

    def _post_with_retry(
        self, webhook: WebhookConfig, payload_bytes: bytes, event_type: str
    ) -> tuple[bool, int | None, str | None]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "mb-crawler-daemon/1.0",
            "X-MB-Event": event_type,
        }
        if webhook.secret:
            headers["X-MB-Signature"] = self._compute_signature(
                webhook.secret, payload_bytes
            )

        last_error: str | None = None
        status_code: int | None = None

        for attempt in range(self.max_retries):
            try:
                r = requests.post(
                    webhook.url,
                    data=payload_bytes,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify_tls,
                )
                status_code = r.status_code
                if r.status_code < 400:
                    log.info(
                        "Webhook delivered successfully to %s (status %d)",
                        webhook.url,
                        r.status_code,
                    )
                    return True, status_code, None
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < self.max_retries - 1:
                delay = 1.0 * (2**attempt)
                log.warning(
                    "Webhook to %s failed (%s) — retrying in %.1fs (attempt %d/%d)",
                    webhook.url,
                    last_error,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        log.error(
            "Failed to deliver webhook to %s after %d attempts: %s",
            webhook.url,
            self.max_retries,
            last_error,
        )
        return False, status_code, last_error

    def test_ping(self, url: str, secret: str | None = None) -> dict[str, Any]:
        """Dispatch a mock test_ping event to verify webhook reachability."""
        test_event = MBEvent(
            event="test_ping",
            data={
                "message": "ManageBac Webhook Test Ping — daemon connection successful!",
                "service": "mb-crawler-daemon",
            },
        )
        wh = WebhookConfig(url=url, secret=secret, events=["*"], enabled=True)
        payload_bytes = test_event.to_json().encode("utf-8")
        success, status_code, error = self._post_with_retry(
            wh, payload_bytes, test_event.event
        )
        return {
            "url": url,
            "success": success,
            "status_code": status_code,
            "error": error,
        }
