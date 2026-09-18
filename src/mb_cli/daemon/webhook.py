"""Webhook dispatcher supporting HTTP POST with signatures and exponential retries.

Delivery is bounded by a wall-clock ceiling that covers request time as well
as backoff sleeps, so one slow endpoint cannot pin the daemon's poll loop. Only
genuinely transient failures are retried: 5xx, 408 and 429. Every other 4xx is
a configuration or credential problem that no backoff can fix. Redirects are
reported, never followed — following one would re-send the signed body and the
signature headers to a different host.

The signature covers the timestamp and the body together::

    signed_material = f"{X-MB-Timestamp}.".encode("utf-8") + request_body
    X-MB-Signature  = "sha256=" + hmac_sha256(secret, signed_material).hexdigest()

Signing the timestamp is what makes the freshness window *authenticated*.
Without it an attacker who captures a single POST can replay it forever by
rewriting only ``X-MB-Timestamp``, because the original signature still
validates over the untouched body and the receiver's freshness check passes.
The ``.`` is a delimiter so ``ts=17`` + ``body="89ab"`` cannot be confused with
``ts=1789`` + ``body="ab"``.

BREAKING PROTOCOL CHANGE: receivers written against the original body-only
construction reject every payload until they are updated. See docs/events.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import requests

from .events import MBEvent, WebhookConfig

log = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="

# Hard ceiling on the WALL-CLOCK time one event may cost across every attempt,
# including request time. It bounds the whole delivery, not just the sleeps
# between attempts: a single attempt may not run longer than what is left of
# the budget either, so a slow endpoint cannot pin the poll loop.
MAX_TOTAL_RETRY_SECONDS = 60.0

# Exponential backoff base between attempts (1s, 2s, 4s, ...).
BACKOFF_BASE_SECONDS = 1.0

_ALLOWED_SCHEMES = ("https", "http")

# Statuses that are worth another attempt. 408 (timeout) and 429 (throttled)
# are the only 4xx that can succeed on a retry; every other 4xx is a
# configuration, credential or routing problem that no backoff can fix.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})
_RETRYABLE_STATUS_RANGES = ((500, 600),)

_MASK = "***"


def _is_retryable_status(status_code: object) -> bool:
    """Whether an HTTP status deserves another attempt."""
    if not isinstance(status_code, int):
        return False
    if status_code in _RETRYABLE_STATUSES:
        return True
    return any(lo <= status_code < hi for lo, hi in _RETRYABLE_STATUS_RANGES)


def signed_material(timestamp: str, payload_bytes: bytes) -> bytes:
    """Return the exact bytes the HMAC covers: ``"<timestamp>." + body``.

    Shared with receiver implementations so producer and consumer cannot drift
    apart on the one thing that makes the freshness check meaningful.
    """
    return f"{timestamp}.".encode("utf-8") + payload_bytes


def _wall_clock_timestamp() -> str:
    """The ``X-MB-Timestamp`` value: Unix seconds to millisecond precision."""
    return f"{time.time():.3f}"


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
        parsed = urlsplit(url)
    except ValueError as exc:
        return f"malformed_url: {exc}"
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"disallowed_scheme:{parsed.scheme}"
    if not parsed.hostname:
        return "missing_host"
    return None


# ── Credential redaction for logs ───────────────────────────────────────
# Push providers routinely put the credential itself in the path or the query
# (Slack `…/services/T…/B…/<token>`, Bark `api.day.app/<key>/…`, WeCom
# `…/send?key=…`). Logging the URL verbatim therefore writes a live token into
# daemon.log, which `daemon install` leaves at default umask permissions.

# Query parameters whose *name* marks the value as a credential.
_SECRETISH_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "api-key",
        "apikey",
        "auth",
        "authorization",
        "device_key",
        "devicekey",
        "key",
        "keys",
        "passwd",
        "password",
        "pwd",
        "secret",
        "sig",
        "sign",
        "signature",
        "token",
        "webhook_key",
    }
)

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _looks_like_token(segment: str) -> bool:
    """Whether a URL segment is a credential rather than a human-readable word.

    Deliberately narrow so ordinary path words (``services``, ``webhook``,
    ``core_tasks``, ``Homework3``) survive redaction and the log line stays
    debuggable. A segment must look like a random credential:

    - 20+ characters of base64/hex/uuid-ish material, OR
    - 8+ characters of UPPERCASE-and-digit material (Slack's ``T0AAAA…`` /
      ``B0BBBB…`` team and bot ids).
    """
    if len(segment) < 8 or not _TOKEN_SHAPE.match(segment):
        return False
    if len(segment) >= 20:
        return True
    has_upper = any(c.isupper() for c in segment)
    has_digit = any(c.isdigit() for c in segment)
    return has_upper and has_digit and segment == segment.upper()


def _redact_path(path: str) -> str:
    """Mask credential-bearing path segments, keeping the path shape."""
    if not path:
        return path
    return "/".join(
        _MASK if seg and _looks_like_token(seg) else seg for seg in path.split("/")
    )


def _redact_query(query: str) -> str:
    """Mask credential-bearing query values, keeping parameter names."""
    if not query:
        return query
    redacted = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.lower() in _SECRETISH_QUERY_KEYS or (
            value and _looks_like_token(value)
        ):
            redacted.append(f"{name}={_MASK}")
        else:
            redacted.append(f"{name}={value}")
    return "&".join(redacted)


def redact_webhook_url(url: str | None, limit: int = 160) -> str:
    """Render a webhook URL safe to log: host and path shape kept, secrets masked.

    ``https://hooks.slack.com/services/T0AAA/B0BBB/xOXpAb…`` becomes
    ``https://hooks.slack.com/services/***/***/***``, and
    ``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<uuid>`` becomes
    ``…/send?key=***``. Any ``user:password@`` userinfo is dropped outright.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        # Unparseable (e.g. a bad IPv6 literal) — refuse to log it at all
        # rather than guess where the credential is.
        return _MASK

    netloc = hostname or ""
    if port is not None:
        netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        netloc = f"{_MASK}@{netloc}" if netloc else _MASK

    redacted = urlunsplit(
        (
            parts.scheme,
            netloc,
            _redact_path(parts.path),
            _redact_query(parts.query),
            "",  # fragment: never meaningful in a webhook URL
        )
    )
    return _sanitize_for_log(redacted, limit)


class WebhookDispatcher:
    """Dispatches MBEvents to configured webhook endpoints.

    ``transport``, ``clock`` and ``sleep`` are seams for deterministic tests:
    they default to :func:`requests.post`, :func:`time.monotonic` and
    :func:`time.sleep`. ``transport`` receives the same keyword arguments
    :func:`requests.post` does and must return an object exposing
    ``status_code``, ``text`` and ``headers``.
    """

    def __init__(
        self,
        webhooks: list[WebhookConfig] | None = None,
        verify_tls: bool = True,
        timeout: float = 10.0,
        max_retries: int = 3,
        total_budget_seconds: float = MAX_TOTAL_RETRY_SECONDS,
        transport: Any | None = None,
        clock: Any | None = None,
        sleep: Any | None = None,
    ):
        self.webhooks = webhooks or []
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.max_retries = max_retries
        self.total_budget_seconds = total_budget_seconds
        self._transport = transport or requests.post
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    @staticmethod
    def _compute_signature(secret: str, timestamp: str, payload_bytes: bytes) -> str:
        """Compute the HMAC-SHA256 signature over ``"<timestamp>." + body``."""
        return SIGNATURE_PREFIX + hmac.new(
            secret.encode("utf-8"),
            signed_material(timestamp, payload_bytes),
            hashlib.sha256,
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
                log.error(
                    "Refusing to dispatch to %s: %s",
                    redact_webhook_url(webhook.url),
                    reason,
                )
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
        """POST one endpoint, with exponential backoff inside a hard ceiling.

        The ceiling is wall-clock and covers request time as well as backoff
        sleeps, so the total cost of one event is bounded no matter how slow the
        endpoint is. Only transient failures are retried.
        """
        display = redact_webhook_url(webhook.url)

        # Wall-clock timestamp for the envelope; signed together with the body
        # so the header a receiver checks for freshness cannot be forged.
        timestamp = _wall_clock_timestamp()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "tahuti-daemon/1.0",
            "X-MB-Event": event_type,
            "X-MB-Timestamp": timestamp,
        }
        if webhook.secret:
            headers["X-MB-Signature"] = self._compute_signature(
                webhook.secret, timestamp, payload_bytes
            )

        last_error: str | None = None
        status_code: int | None = None
        attempts = 0
        started = self._clock()

        for attempt in range(1, max(1, self.max_retries) + 1):
            attempts = attempt
            remaining = self.total_budget_seconds - (self._clock() - started)
            if remaining <= 0:
                last_error = last_error or "delivery_budget_exhausted"
                log.error(
                    "Aborting webhook to %s after %d attempt(s): exhausted the "
                    "%.1fs delivery budget",
                    display,
                    attempt - 1,
                    self.total_budget_seconds,
                )
                break

            try:
                # The request timeout is clamped to the remaining budget so the
                # *last* attempt cannot outrun the ceiling either.
                r = self._transport(
                    url=webhook.url,
                    data=payload_bytes,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    verify=self.verify_tls,
                    # A 3xx must not be followed: `requests` would replay the
                    # signed body and the signature headers to a *different*
                    # host (307) or downgrade https to http (301/302), which is
                    # exactly what signing is meant to prevent.
                    allow_redirects=False,
                )
                status_code = getattr(r, "status_code", None)
                body = getattr(r, "text", "") or ""

                if isinstance(status_code, int) and status_code < 300:
                    log.info(
                        "Webhook delivered to %s (status %d, %d attempt(s))",
                        display,
                        status_code,
                        attempt,
                    )
                    return True, status_code, None

                last_error = _sanitize_for_log(f"HTTP {status_code}: {body}")

                if isinstance(status_code, int) and 300 <= status_code < 400:
                    # The endpoint has moved — that is a config fix, not a
                    # backoff problem.
                    location = _sanitize_for_log(
                        str((getattr(r, "headers", None) or {}).get("Location", "")),
                        120,
                    )
                    last_error = f"HTTP {status_code}: redirect_not_followed"
                    if location:
                        last_error += f" (Location: {location})"
                    log.error(
                        "Webhook to %s returned %d and is not being followed: "
                        "redirecting would re-send the signed payload to a new "
                        "host (%s). Fix the configured URL instead.",
                        display,
                        status_code,
                        redact_webhook_url(location) or "unknown",
                    )
                    break

                if not _is_retryable_status(status_code):
                    log.error(
                        "Webhook to %s failed permanently with HTTP %s — "
                        "retrying a non-retryable status would only delay the "
                        "diagnosis",
                        display,
                        status_code,
                    )
                    break
            except Exception as exc:
                # Network-level fault: retryable.
                last_error = _sanitize_for_log(str(exc))

            if attempt < max(1, self.max_retries):
                remaining = self.total_budget_seconds - (self._clock() - started)
                if remaining <= 0:
                    last_error = last_error or "delivery_budget_exhausted"
                    log.error(
                        "Aborting webhook to %s after %d attempt(s): exhausted "
                        "the %.1fs delivery budget",
                        display,
                        attempt,
                        self.total_budget_seconds,
                    )
                    break
                # Backoff is clamped to the remaining budget so the sleeps
                # alone can never push the total past the ceiling.
                delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), remaining)
                log.warning(
                    "Webhook to %s failed (%s) — retrying in %.1fs "
                    "(attempt %d/%d, %.1fs of the %.1fs budget used)",
                    display,
                    last_error,
                    delay,
                    attempt,
                    self.max_retries,
                    self._clock() - started,
                    self.total_budget_seconds,
                )
                self._sleep(delay)

        log.error(
            "Failed to deliver webhook to %s after %d attempt(s): %s",
            display,
            attempts,
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
