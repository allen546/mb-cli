"""Webhook dispatcher supporting HTTP POST with signatures and exponential retries."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlparse
import requests

from .events import MBEvent, WebhookConfig

log = logging.getLogger(__name__)

# Hard ceiling on total time spent retrying one event, so a slow or hostile
# endpoint cannot pin the daemon's thread indefinitely.
MAX_TOTAL_RETRY_SECONDS = 60.0
_ALLOWED_SCHEMES = ("https", "http")


def _sanitize_for_log(text: str | None, limit: int = 120) -> str:
    """Render a remote-supplied string safely for logs and JSON results.

    Strips control characters so a hostile endpoint's response body cannot
    forge log lines or inject ANSI/OSC escapes into a terminal.
    """
    if not text:
        return ""
    cleaned = "".join(
        ch for ch in str(text) if ch == "\t" or (0x20 <= ord(ch) != 0x7F)
    )
    return cleaned[:limit]


def _validate_webhook_url(url: str) -> str | None:
    """Return a reason string if the webhook URL is unusable, else None.

    Only http/https are accepted — this keeps ``file://`` and other schemes
    from being handed to requests.
    """
    if not url:
        return "empty_url"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"malformed_url: {exc}"
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"disallowed_scheme:{parsed.scheme}"
    if not parsed.hostname:
        return "missing_host"
    return None


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
        payload_bytes = event.to_json().encode("utf-8")

        for webhook in self.webhooks:
            if not webhook.matches_event(event.event):
                continue

            reason = _validate_webhook_url(webhook.url)
            if reason:
                log.error("Refusing to dispatch to %s: %s", webhook.url, reason)
                results.append(
                    {
                        "url": webhook.url,
                        "event": event.event,
                        "event_id": event.event_id,
                        "success": False,
                        "status_code": None,
                        "error": f"invalid_webhook_url:{reason}",
                    }
                )
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
            "User-Agent": "tahuti-daemon/1.0",
            "X-MB-Event": event_type,
            "X-MB-Timestamp": f"{time.time():.3f}",
        }
        if webhook.secret:
            headers["X-MB-Signature"] = self._compute_signature(
                webhook.secret, payload_bytes
            )

        last_error: str | None = None
        status_code: int | None = None
        started = time.monotonic()

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
                # Sanitize the remote body before it reaches logs or JSON output.
                last_error = _sanitize_for_log(
                    f"HTTP {r.status_code}: {r.text}"
                )
            except Exception as exc:
                last_error = _sanitize_for_log(str(exc))

            if attempt < self.max_retries - 1:
                delay = 1.0 * (2**attempt)
                elapsed = time.monotonic() - started
                if elapsed + delay > MAX_TOTAL_RETRY_SECONDS:
                    log.error(
                        "Aborting retries for %s after %.1fs total",
                        webhook.url,
                        elapsed,
                    )
                    break
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
                "service": "tahuti-daemon",
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
