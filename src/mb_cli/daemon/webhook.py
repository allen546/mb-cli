"""Webhook dispatcher supporting HTTP POST with signatures and exponential retries.

Wire contract
-------------
Every delivery is a ``POST`` of the raw ``MBEvent`` JSON with these headers:

- ``X-MB-Event``      — the event type
- ``X-MB-Timestamp``  — Unix seconds, 3 decimal places, as a string
- ``X-MB-Signature``  — ``sha256=<hex hmac>``, omitted only when no secret is
  configured (see :data:`OUTCOME_SUCCESS` consumers below)

The signature covers **the timestamp and the body together**::

    signed_material = f"{X-MB-Timestamp}.".encode("utf-8") + request_body
    X-MB-Signature  = "sha256=" + hmac_sha256(secret, signed_material).hexdigest()

The ``.`` is a delimiter so ``ts=17`` + ``body="89ab"`` cannot be confused with
``ts=1789`` + ``body="ab"``. Signing the timestamp is what makes the freshness
window *authenticated*: without it an attacker who captures a single POST can
replay it forever by rewriting only the ``X-MB-Timestamp`` header, because the
original signature still validates over the untouched body. A receiver that
enforces freshness but ignores this construction has no replay protection at all.

This is a **breaking protocol change** for receivers written against the
original body-only construction; see ``docs/events.md`` for the migration note.

Delivery outcomes
-----------------
:meth:`WebhookDispatcher.dispatch` returns one result dict per *configured*
endpoint (not per delivered endpoint), each carrying a machine-readable
:data:`OUTCOME_SUCCESS` / :data:`OUTCOME_PERMANENT_FAILURE` /
:data:`OUTCOME_TRANSIENT_FAILURE` ``outcome`` plus a ``retryable`` flag. A bare
``success`` boolean collapses "delivered", "will never work" and "try again
later" into one value, which is what let a caller mark an event handled as soon
as *one* endpoint succeeded while a second stayed down forever.
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

# ── Per-endpoint delivery outcomes ──────────────────────────────────────
# Machine-readable so a caller can tell "handled" from "try again later"
# without re-deriving it from a status code.
OUTCOME_SUCCESS = "success"
OUTCOME_PERMANENT_FAILURE = "permanent_failure"
OUTCOME_TRANSIENT_FAILURE = "transient_failure"

SIGNATURE_PREFIX = "sha256="

# Hard ceiling on the wall-clock time ONE event may cost across every attempt,
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
# 5xx is a server-side fault and is retryable in full.
_RETRYABLE_STATUS_RANGES = ((500, 600),)

_MASK = "***"


def signed_material(timestamp: str, payload_bytes: bytes) -> bytes:
    """Return the exact bytes the HMAC covers: ``"<timestamp>." + body``.

    Shared with receiver implementations so producer and consumer cannot drift
    apart on the one thing that makes the freshness check meaningful.
    """
    return f"{timestamp}.".encode("utf-8") + payload_bytes


def _wall_clock_timestamp() -> str:
    """The ``X-MB-Timestamp`` value: Unix seconds to millisecond precision."""
    return f"{time.time():.3f}"


def _is_retryable_status(status_code: Any) -> bool:
    """Whether an HTTP status deserves another attempt."""
    if not isinstance(status_code, int):
        return False
    if status_code in _RETRYABLE_STATUSES:
        return True
    return any(lo <= status_code < hi for lo, hi in _RETRYABLE_STATUS_RANGES)


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
        # Endpoints already nagged about a missing secret, so a daemon that
        # polls every 30s does not emit the same error on every single event.
        self._unsigned_warned: set[str] = set()

    # ── Signing ─────────────────────────────────────────────────────────
    @staticmethod
    def _compute_signature(secret: str, timestamp: str, payload_bytes: bytes) -> str:
        """Compute the HMAC-SHA256 signature over ``"<timestamp>." + body``."""
        return SIGNATURE_PREFIX + hmac.new(
            secret.encode("utf-8"),
            signed_material(timestamp, payload_bytes),
            hashlib.sha256,
        ).hexdigest()

    # ── Result shape ────────────────────────────────────────────────────
    @staticmethod
    def _result(
        url: str,
        event: str,
        event_id: str | None,
        outcome: str,
        status_code: int | None = None,
        error: str | None = None,
        attempts: int = 0,
        signed: bool = False,
        retryable: bool = False,
    ) -> dict[str, Any]:
        """Build one per-endpoint result dict.

        ``url`` is preserved verbatim for programmatic use; ``url_display`` is
        the redacted form for anything a human or a log file will see.
        """
        return {
            "url": url,
            "url_display": redact_webhook_url(url),
            "event": event,
            "event_id": event_id,
            "outcome": outcome,
            "success": outcome == OUTCOME_SUCCESS,
            "retryable": retryable,
            "signed": signed,
            "status_code": status_code,
            "error": error,
            "attempts": attempts,
        }

    def _warn_unsigned(self, url: str) -> None:
        """Complain loudly, once per endpoint, about unsigned payloads."""
        if url in self._unsigned_warned:
            return
        self._unsigned_warned.add(url)
        log.error(
            "Webhook %s has NO secret configured — every payload is sent "
            "UNSIGNED and any receiver that verifies X-MB-Signature will "
            "reject it. Set a secret in daemon.json to authenticate deliveries.",
            redact_webhook_url(url),
        )

    def dispatch(self, event: MBEvent) -> list[dict[str, Any]]:
        """Dispatch an event to all matching enabled webhooks.

        Returns one result per *configured* endpoint, in configuration order,
        including the ones that never got a request because their URL is
        invalid. Callers must not collapse this list with ``any(...)`` — see
        :func:`retryable_results`.
        """
        results: list[dict[str, Any]] = []
        # Computed once and reused for every endpoint: the body must be
        # byte-identical across endpoints or the signatures would not describe
        # the payload the receivers actually see.
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
                    self._result(
                        url=webhook.url,
                        event=event.event,
                        event_id=event.event_id,
                        outcome=OUTCOME_PERMANENT_FAILURE,
                        error=f"invalid_webhook_url:{reason}",
                        signed=bool(webhook.secret),
                    )
                )
                continue

            results.append(
                self._post_with_retry(
                    webhook, payload_bytes, event.event, event.event_id
                )
            )

        return results

    def retry_failed(self, event: MBEvent, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Re-attempt only the endpoints in ``results`` that still owe the event.

        Endpoints that already delivered, and endpoints that failed
        permanently, are skipped — retrying them would re-notify a working
        receiver or re-send to a URL that can never work. Each retried endpoint
        gets a fresh timestamp, a fresh signature and a fresh time budget.
        """
        pending = retryable_results(results)
        if not pending:
            return []
        payload_bytes = event.to_json().encode("utf-8")
        by_url = {w.url: w for w in self.webhooks}
        retried: list[dict[str, Any]] = []
        for previous in pending:
            webhook = by_url.get(previous.get("url", ""))
            if webhook is None:
                # Not a configured endpoint of this dispatcher (e.g. a
                # `test_ping` result) — there is no config to retry with.
                continue
            retried.append(
                self._post_with_retry(
                    webhook, payload_bytes, event.event, event.event_id
                )
            )
        return retried

    def _post_with_retry(
        self,
        webhook: WebhookConfig,
        payload_bytes: bytes,
        event_type: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """POST one endpoint, with exponential backoff inside a hard ceiling.

        The ceiling is wall-clock and covers request time as well as backoff
        sleeps, so the total cost of one event is bounded no matter how many
        endpoints are configured or how slow they are.
        """
        url = webhook.url
        display = redact_webhook_url(url)

        # Wall-clock timestamp for the envelope; signed together with the body.
        timestamp = _wall_clock_timestamp()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "tahuti-daemon/1.0",
            "X-MB-Event": event_type,
            "X-MB-Timestamp": timestamp,
        }
        signed = bool(webhook.secret)
        if signed:
            headers["X-MB-Signature"] = self._compute_signature(
                webhook.secret, timestamp, payload_bytes
            )
        else:
            self._warn_unsigned(url)

        last_error: str | None = None
        status_code: int | None = None
        outcome = OUTCOME_TRANSIENT_FAILURE
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
                    url=url,
                    data=payload_bytes,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    verify=self.verify_tls,
                    allow_redirects=False,
                )
                status_code = getattr(r, "status_code", None)
                body = getattr(r, "text", "") or ""

                if isinstance(status_code, int) and status_code < 300:
                    log.info(
                        "Webhook delivered to %s (status %d, %d attempt(s), signed=%s)",
                        display,
                        status_code,
                        attempt,
                        signed,
                    )
                    return self._result(
                        url=url,
                        event=event_type,
                        event_id=event_id,
                        outcome=OUTCOME_SUCCESS,
                        status_code=status_code,
                        attempts=attempt,
                        signed=signed,
                    )

                last_error = _sanitize_for_log(f"HTTP {status_code}: {body}")

                if isinstance(status_code, int) and 300 <= status_code < 400:
                    # A 3xx is reported, never followed: `requests` would
                    # replay the signed body and the signature headers to a
                    # *different* host (307) or downgrade https to http
                    # (301/302), which is exactly what signing is meant to
                    # prevent. The endpoint has moved — that is a config fix.
                    outcome = OUTCOME_PERMANENT_FAILURE
                    location = _sanitize_for_log(
                        str(
                            (getattr(r, "headers", None) or {}).get("Location", "")
                        ),
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
                    outcome = OUTCOME_PERMANENT_FAILURE
                    log.error(
                        "Webhook to %s failed permanently with HTTP %s — "
                        "retrying a non-retryable status would only delay the "
                        "diagnosis (status %s, %d attempt(s))",
                        display,
                        status_code,
                        status_code,
                        attempt,
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
                delay = min(
                    BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), remaining
                )
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
            "Failed to deliver webhook to %s after %d attempt(s) [%s]: %s",
            display,
            attempts,
            outcome,
            last_error,
        )
        return self._result(
            url=url,
            event=event_type,
            event_id=event_id,
            outcome=outcome,
            status_code=status_code,
            error=last_error,
            attempts=attempts,
            signed=signed,
            retryable=outcome == OUTCOME_TRANSIENT_FAILURE,
        )

    def test_ping(self, url: str, secret: str | None = None) -> dict[str, Any]:
        """Dispatch a mock test_ping event to verify webhook reachability."""
        # Same URL guard as a real dispatch. `test_ping` used to skip it, so
        # the one command where a user is most likely to paste a wrong URL was
        # the one place a bad scheme reached `requests` and surfaced as a
        # confusing network error instead of "your URL is wrong".
        reason = _validate_webhook_url(url)
        test_event = MBEvent(
            event="test_ping",
            data={
                "message": "ManageBac Webhook Test Ping — daemon connection successful!",
                "service": "tahuti-daemon",
            },
        )
        if reason:
            log.error(
                "Refusing to test webhook %s: %s",
                redact_webhook_url(url),
                reason,
            )
            return self._result(
                url=url,
                event=test_event.event,
                event_id=test_event.event_id,
                outcome=OUTCOME_PERMANENT_FAILURE,
                error=f"invalid_webhook_url:{reason}",
                signed=bool(secret),
            )

        wh = WebhookConfig(url=url, secret=secret, events=["*"], enabled=True)
        payload_bytes = test_event.to_json().encode("utf-8")
        return self._post_with_retry(
            wh, payload_bytes, test_event.event, test_event.event_id
        )


def retryable_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the per-endpoint results that a caller should attempt again.

    This is the primitive that makes per-endpoint at-least-once implementable:
    an event is "handled" when this list is empty, and every entry in it names
    an endpoint that still owes the event. Endpoints that failed *permanently*
    are deliberately excluded — re-sending to a typo'd URL on every poll would
    be a notification storm, not a retry.
    """
    return [r for r in results if r.get("retryable")]


def all_delivered(results: list[dict[str, Any]]) -> bool:
    """True when every configured endpoint delivered, i.e. no work is owed.

    An empty result list (no endpoints configured, or a dry run) counts as
    delivered — that is the pre-existing contract callers rely on.
    """
    return all(r.get("success") for r in results)
