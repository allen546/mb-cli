"""Tests for the webhook dispatcher.

Covers the signed-material construction (the timestamp is inside the HMAC, not a
sibling header), credential redaction before logging, the wall-clock delivery
ceiling, non-retryable status handling, per-endpoint delivery outcomes, the
unsigned-payload warning, `test_ping` URL validation, and redirect refusal.


The signature covers ``"<X-MB-Timestamp>." + body``. When it covered the body
alone, ``X-MB-Timestamp`` was an unauthenticated sibling header, so anyone who
captured one POST could replay it indefinitely by rewriting that header: the
original digest still validated and the receiver's freshness check passed.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import logging
from pathlib import Path
import time

import pytest
import requests
import requests_mock

from mb_cli.daemon import webhook as webhook_module
from mb_cli.daemon.events import MBEvent, WebhookConfig
from mb_cli.daemon.webhook import (
    OUTCOME_PERMANENT_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_TRANSIENT_FAILURE,
    WebhookDispatcher,
    all_delivered,
    redact_webhook_url,
    retryable_results,
    signed_material,
)

FAKE_SECRET = "test-secret-not-real"
# Not a credential: this is the throwaway placeholder Slack publishes in its docs.
SLACK_URL = (
    "https://hooks.slack.com/services/"
    "T0AAAAAAAA/B0BBBBBBB/xOXpAbCdEfGhIjKlMnOpQrStU"
)
BARK_URL = "https://api.day.app/ctAbCdEfGhIjKlMnOpQrSt/Homework%203/Finish%20Essay"
WECOM_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    "?key=693a91f6-7adc-4a1c-a7d2-0123456789ab"
)

_RECEIVER_PATH = (
    Path(__file__).resolve().parent.parent
    / "extras"
    / "mb-notifier"
    / "bark_webhook_receiver.py"
)


def _load_reference_receiver():
    """Import extras/mb-notifier/bark_webhook_receiver.py from source.

    The receiver is the reference implementation of the wire contract, so the
    dispatcher's tests verify against *it* rather than a copy of the
    verification logic — that is what stops producer and consumer from drifting
    apart on the signature construction.
    """
    root = logging.getLogger()
    prev_level, prev_handlers = root.level, list(root.handlers)
    try:
        spec = importlib.util.spec_from_file_location(
            "bark_webhook_receiver", _RECEIVER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        # The receiver calls logging.basicConfig() at import time; undo any
        # global logging mutation so importing it cannot leak into other tests.
        root.setLevel(prev_level)
        for handler in list(root.handlers):
            if handler not in prev_handlers:
                root.removeHandler(handler)
    return module


def _event(event: str = "deadline_approaching") -> MBEvent:
    return MBEvent(
        event=event,
        data={"task_id": "999", "title": "Final Project"},
    )


def _as_bytes(body) -> bytes:
    return body if isinstance(body, bytes) else body.encode("utf-8")


def test_signature_covers_timestamp_and_body():
    """The signature is over "<X-MB-Timestamp>.<body>", not the body alone."""
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh])

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        dispatcher.dispatch(_event())

    req = m.last_request
    ts = req.headers["X-MB-Timestamp"]
    expected = "sha256=" + hmac.new(
        FAKE_SECRET.encode("utf-8"),
        signed_material(ts, _as_bytes(req.body)),
        hashlib.sha256,
    ).hexdigest()
    assert req.headers["X-MB-Signature"] == expected
    # The header is the exact string that was signed, not a reformatting of it.
    assert ts == f"{float(ts):.3f}"


def test_payload_is_byte_identical_across_endpoints_for_one_event():
    """One event, many endpoints: one body, one signature per request.

    The body is computed once in `dispatch` and reused, so every endpoint is
    asked to verify the same bytes. The timestamp is per *request*, which is
    correct for freshness — a later endpoint must not inherit an earlier one's
    delivery time.
    """
    urls = ["http://localhost:8888/a", "http://localhost:8888/b"]
    webs = [WebhookConfig(url=u, secret=FAKE_SECRET) for u in urls]
    event = _event()

    with requests_mock.Mocker() as m:
        for u in urls:
            m.post(u, status_code=200)
        WebhookDispatcher(webhooks=webs).dispatch(event)

    assert len(m.request_history) == 2
    bodies = {_as_bytes(r.body) for r in m.request_history}
    assert bodies == {event.to_json().encode("utf-8")}

    # Each request's signature verifies against its own timestamp header.
    receiver = _load_reference_receiver()
    for req in m.request_history:
        ok, reason = receiver.verify_signature(
            FAKE_SECRET,
            req.headers["X-MB-Signature"],
            _as_bytes(req.body),
            req.headers["X-MB-Timestamp"],
        )
        assert (ok, reason) == (True, "ok"), reason


def test_replayed_request_with_rewritten_timestamp_is_rejected():
    """The replay this construction exists to stop: swap the timestamp, keep the signature.

    An attacker who captured one POST rewrites only ``X-MB-Timestamp`` to *now*
    and replays. Under the old body-only signature the captured digest still
    validated and the receiver's freshness check waved the replay through; here
    the digest no longer matches, because the timestamp is signed material.
    """
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh])

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        dispatcher.dispatch(_event())

    req = m.last_request
    body = _as_bytes(req.body)
    captured_signature = req.headers["X-MB-Signature"]

    receiver = _load_reference_receiver()
    receiver.reset_replay_cache()

    # The genuine request still verifies.
    ok, reason = receiver.verify_signature(
        FAKE_SECRET, captured_signature, body, req.headers["X-MB-Timestamp"]
    )
    assert (ok, reason) == (True, "ok"), reason

    # The replay, restamped to *now* so it is inside the freshness window: this
    # is the case the old construction could not catch.
    for offset in (0.0, -120.0, 120.0):
        forged = f"{time.time() + offset:.3f}"
        ok, reason = receiver.verify_signature(FAKE_SECRET, captured_signature, body, forged)
        assert ok is False, "a fresh restamp of the captured payload was accepted"
        assert reason == "signature_mismatch", reason

    # A wildly stale restamp is rejected on the digest too, not just on age.
    ok, reason = receiver.verify_signature(
        FAKE_SECRET, captured_signature, body, "1700000000.000"
    )
    assert ok is False
    assert reason == "signature_mismatch", reason


def test_reference_receiver_accepts_exactly_what_the_dispatcher_signs():
    """Producer and the bundled receiver agree on the wire format."""
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    event = _event("task_created")

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        WebhookDispatcher(webhooks=[wh]).dispatch(event)

    req = m.last_request
    body = _as_bytes(req.body)

    receiver = _load_reference_receiver()
    assert body == event.to_json().encode("utf-8"), "payload must be byte-exact"
    ok, reason = receiver.verify_signature(
        FAKE_SECRET,
        req.headers["X-MB-Signature"],
        body,
        req.headers["X-MB-Timestamp"],
    )
    assert (ok, reason) == (True, "ok"), reason


def test_signature_material_has_no_delimiter_ambiguity():
    """ts="17"+body="89ab" must not collide with ts="1789"+body="ab"."""
    assert signed_material("17", b"89ab") != signed_material("1789", b"ab")
    assert signed_material("1789.5", b"{}") == b"1789.5.{}"


def test_signature_changes_when_the_timestamp_changes(monkeypatch):
    """Two deliveries of an identical event must not share a digest.

    A shared digest is what makes a capture reusable forever: the signature has
    to be bound to the moment of delivery for a freshness window to mean
    anything.
    """
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh])
    event = _event()
    event.event_id = "evt_fixed000001"
    event.timestamp = "2026-09-19T00:00:00+00:00"

    # Both timestamps must sit inside the receiver's freshness window, so the
    # digests are compared on signature grounds and not rejected as stale.
    base = time.time()
    captured = []
    for seconds in (f"{base:.3f}", f"{base + 60:.3f}"):
        monkeypatch.setattr(
            webhook_module, "_wall_clock_timestamp", lambda s=seconds: s
        )
        with requests_mock.Mocker() as m:
            m.post(wh.url, status_code=200)
            dispatcher.dispatch(event)
        captured.append(
            (
                m.last_request.headers["X-MB-Timestamp"],
                m.last_request.headers["X-MB-Signature"],
            )
        )

    assert float(captured[0][0]) < float(captured[1][0])
    assert captured[0][1] != captured[1][1], "the digest must be per-delivery"

    # And each one verifies against its own timestamp, per the documented rule.
    receiver = _load_reference_receiver()
    body = event.to_json().encode("utf-8")
    for ts, signature in captured:
        ok, reason = receiver.verify_signature(FAKE_SECRET, signature, body, ts)
        assert (ok, reason) == (True, "ok"), reason
    # Cross-verification must fail: a signature for ts A does not cover ts B.
    ok, reason = receiver.verify_signature(
        FAKE_SECRET, captured[0][1], body, captured[1][0]
    )
    assert ok is False and reason == "signature_mismatch"


# ── Credential redaction before logging ─────────────────────────────────


def test_redact_webhook_url_masks_provider_credentials():
    assert redact_webhook_url(SLACK_URL) == (
        "https://hooks.slack.com/services/***/***/***"
    )
    assert redact_webhook_url(BARK_URL) == (
        "https://api.day.app/***/Homework%203/Finish%20Essay"
    )
    assert redact_webhook_url(WECOM_URL) == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***"
    )


def test_redact_webhook_url_keeps_host_and_path_shape():
    """Redaction must not make the log line useless."""
    url = "https://example.test/hook/v2/debug?attempt=3"
    assert redact_webhook_url(url) == url


def test_redact_webhook_url_drops_userinfo_and_unparseable_hosts():
    assert redact_webhook_url("https://user:pw@example.test/hook") == (
        "https://***@example.test/hook"
    )
    assert "pw" not in redact_webhook_url("https://user:pw@example.test/hook")
    # An unparseable URL is not guessed at — it is masked wholesale.
    assert redact_webhook_url("https://[::1/hook") == "***"
    assert redact_webhook_url("") == ""


def test_slack_token_is_never_written_to_the_log(caplog):
    wh = WebhookConfig(url=SLACK_URL, secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh])

    with requests_mock.Mocker() as m:
        m.post(SLACK_URL, status_code=200)
        with caplog.at_level(logging.DEBUG, logger="mb_cli.daemon.webhook"):
            dispatcher.dispatch(_event())

    logged = caplog.text
    for secret_fragment in ("T0AAAAAAAA", "B0BBBBBBB", "xOXpAbCdEfGhIjKlMnOpQrStU"):
        assert secret_fragment not in logged, (
            f"{secret_fragment} leaked into the log:\n{logged}"
        )
    assert "hooks.slack.com" in logged, "the endpoint must stay identifiable"


def test_credential_is_not_logged_on_failure_or_refusal(caplog):
    """Every log site, including the retry and refusal paths, is redacted."""
    wh = WebhookConfig(url=BARK_URL, secret=None)
    dispatcher = WebhookDispatcher(webhooks=[wh], max_retries=2)

    with requests_mock.Mocker() as m:
        m.post(BARK_URL, status_code=503, text="upstream down")
        with caplog.at_level(logging.DEBUG, logger="mb_cli.daemon.webhook"):
            dispatcher.dispatch(_event())

    assert "ctAbCdEfGhIjKlMnOpQrSt" not in caplog.text

    bad = WebhookConfig(
        url="ftp://api.day.app/ctAbCdEfGhIjKlMnOpQrSt/hook", secret=FAKE_SECRET
    )
    with requests_mock.Mocker() as m:
        with caplog.at_level(logging.DEBUG, logger="mb_cli.daemon.webhook"):
            WebhookDispatcher(webhooks=[bad]).dispatch(_event())
    assert "ctAbCdEfGhIjKlMnOpQrSt" not in caplog.text


# ── A real wall-clock ceiling, and no retrying the unfixable ─────────────


class _FakeClock:
    """A monotonic clock driven entirely by the code under test."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _hanging_transport(clock: _FakeClock, calls: list | None = None):
    """An endpoint that burns its entire timeout and then times out."""

    def transport(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        clock.advance(kwargs["timeout"])
        raise requests.Timeout("simulated endpoint hang")

    return transport


def test_delivery_time_respects_the_wall_clock_ceiling():
    """Request time counts against the budget, not just the backoff sleeps."""
    budget = 20.0
    clock = _FakeClock()
    calls: list[dict] = []
    dispatcher = WebhookDispatcher(
        webhooks=[WebhookConfig(url="http://slow.test/hook", secret=FAKE_SECRET)],
        timeout=10.0,
        max_retries=8,
        total_budget_seconds=budget,
        transport=_hanging_transport(clock, calls),
        clock=clock,
        sleep=clock.sleep,
    )

    results = dispatcher.dispatch(_event())
    elapsed = clock.now - 1_000.0

    assert elapsed <= budget, f"delivery took {elapsed}s of a {budget}s ceiling"
    assert results[0]["outcome"] == OUTCOME_TRANSIENT_FAILURE
    assert results[0]["success"] is False
    # No single attempt may run longer than the ceiling either — the final
    # attempt in particular used to get its full timeout regardless.
    assert calls, "the endpoint must have been attempted"
    assert all(c["timeout"] <= budget for c in calls)
    assert calls[-1]["timeout"] < 10.0, "the last attempt must be clamped"
    # The timeouts shrink as the budget is consumed, i.e. it is a real ceiling.
    assert calls[-1]["timeout"] <= calls[0]["timeout"]
    # The retry sequence was cut short by the ceiling, not by max_retries.
    assert len(calls) < 8


def test_final_attempt_cannot_outrun_the_ceiling():
    """The ceiling used to be tested only *before* a sleep, so the final
    attempt always ran its full 10s timeout regardless of the budget left."""
    budget = 25.0
    clock = _FakeClock()
    calls: list[dict] = []
    dispatcher = WebhookDispatcher(
        webhooks=[WebhookConfig(url="http://slow.test/hook", secret=FAKE_SECRET)],
        timeout=10.0,
        max_retries=3,
        total_budget_seconds=budget,
        transport=_hanging_transport(clock, calls),
        clock=clock,
        sleep=clock.sleep,
    )

    dispatcher.dispatch(_event())

    assert clock.now - 1_000.0 <= budget, "the ceiling must hold"
    assert calls[-1]["timeout"] < 10.0, "the last attempt must be clamped"
    assert clock.now - 1_000.0 == budget, "the ceiling is reached exactly"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_permanent_4xx_is_not_retried(status):
    """No backoff can fix a 4xx, so it must not be paid for."""
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh], max_retries=3)

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=status, text="nope")
        results = dispatcher.dispatch(_event())

    assert m.call_count == 1, f"HTTP {status} must not be retried"
    assert results[0]["outcome"] == OUTCOME_PERMANENT_FAILURE
    assert results[0]["success"] is False
    assert results[0]["retryable"] is False
    assert results[0]["attempts"] == 1
    assert results[0]["status_code"] == status


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status):
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    clock = _FakeClock()
    dispatcher = WebhookDispatcher(
        webhooks=[wh], max_retries=2, clock=clock, sleep=clock.sleep
    )

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=status, text="transient")
        results = dispatcher.dispatch(_event())

    assert m.call_count == 2, f"HTTP {status} should be retried"
    assert results[0]["outcome"] == OUTCOME_TRANSIENT_FAILURE
    assert results[0]["retryable"] is True


def test_backoff_is_exponential():
    clock = _FakeClock()
    dispatcher = WebhookDispatcher(
        webhooks=[WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)],
        max_retries=4,
        clock=clock,
        sleep=clock.sleep,
    )

    with requests_mock.Mocker() as m:
        m.post(dispatcher.webhooks[0].url, status_code=500, text="boom")
        dispatcher.dispatch(_event())

    assert clock.slept == [1.0, 2.0, 4.0]


# ── Redirects must not carry the signed body off-host ───────────────────


def test_redirect_is_not_followed_with_the_signed_body():
    """A 307 must not re-POST the signature headers to a different host."""
    target = "https://evil.example.com/hook"
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[wh], max_retries=1)

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=307, headers={"Location": target})
        # Registered so that, if the bug regressed, the replay is intercepted
        # instead of reaching the network — and therefore observable.
        m.post(target, status_code=200)
        results = dispatcher.dispatch(_event())

    off_host = [r for r in m.request_history if r.url == target]
    assert off_host == [], "the signed body was re-sent off-host"
    assert m.call_count == 1
    assert results[0]["success"] is False
    assert results[0]["outcome"] == OUTCOME_PERMANENT_FAILURE
    assert results[0]["retryable"] is False
    assert "redirect_not_followed" in results[0]["error"]


def test_redirect_location_is_reported_so_the_config_can_be_fixed():
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    with requests_mock.Mocker() as m:
        m.post(
            wh.url,
            status_code=301,
            headers={"Location": "https://hooks.example.test/v2/hook"},
        )
        results = WebhookDispatcher(webhooks=[wh]).dispatch(_event())

    assert results[0]["error"] is not None
    assert "redirect_not_followed" in results[0]["error"]
    # The host is named so the config can be corrected, but a credential in the
    # redirect target is not echoed into the result.
    assert "hooks.example.test" in results[0]["error"]
    assert "sekrit" not in results[0]["error"]
    assert results[0]["status_code"] == 301


def test_redirect_target_credentials_are_redacted_in_the_result():
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    with requests_mock.Mocker() as m:
        m.post(
            wh.url,
            status_code=302,
            headers={
                "Location": (
                    "https://api.day.app/ctAbCdEfGhIjKlMnOpQrSt/hook"
                    "?key=693a91f6-7adc-4a1c-a7d2-0123456789ab"
                )
            },
        )
        results = WebhookDispatcher(webhooks=[wh]).dispatch(_event())

    assert "ctAbCdEfGhIjKlMnOpQrSt" not in results[0]["error"]
    assert "693a91f6" not in results[0]["error"]
    assert "api.day.app" in results[0]["error"]


# ── Per-endpoint outcomes, not one collapsed boolean ─────────────────────


def test_each_endpoint_reports_its_own_outcome():
    """One down endpoint must be visible even when another succeeds."""
    good = WebhookConfig(url="http://localhost:8888/ok", secret=FAKE_SECRET)
    bad = WebhookConfig(url="http://localhost:8888/broken", secret=FAKE_SECRET)
    typo = WebhookConfig(url="ftp://nope/hook", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[good, bad, typo], max_retries=1)

    with requests_mock.Mocker() as m:
        m.post(good.url, status_code=200)
        m.post(bad.url, status_code=404, text="gone")
        results = dispatcher.dispatch(_event())

    assert [r["url"] for r in results] == [good.url, bad.url, typo.url]
    assert [r["outcome"] for r in results] == [
        OUTCOME_SUCCESS,
        OUTCOME_PERMANENT_FAILURE,
        OUTCOME_PERMANENT_FAILURE,
    ]
    # The point of the defect: `any()` over this list would have reported
    # success while two of three endpoints never received the event.
    assert any(r["success"] for r in results)
    assert not all_delivered(results)
    assert retryable_results(results) == []


def test_transient_endpoint_failure_is_flagged_for_retry():
    good = WebhookConfig(url="http://localhost:8888/ok", secret=FAKE_SECRET)
    flaky = WebhookConfig(url="http://localhost:8888/flaky", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[good, flaky], max_retries=1)

    with requests_mock.Mocker() as m:
        m.post(good.url, status_code=200)
        m.post(flaky.url, status_code=503, text="unavailable")
        results = dispatcher.dispatch(_event())

    owed = retryable_results(results)
    assert [r["url"] for r in owed] == [flaky.url]
    assert all_delivered(results) is False
    assert any(r["success"] for r in results) is True


def test_retry_failed_reattempts_only_the_endpoint_that_is_owed():
    """The primitive service.py needs to stop abandoning a failed endpoint."""
    good = WebhookConfig(url="http://localhost:8888/ok", secret=FAKE_SECRET)
    flaky = WebhookConfig(url="http://localhost:8888/flaky", secret=FAKE_SECRET)
    dispatcher = WebhookDispatcher(webhooks=[good, flaky], max_retries=1)

    with requests_mock.Mocker() as m:
        m.post(good.url, status_code=200)
        m.post(flaky.url, status_code=503, text="unavailable")
        first = dispatcher.dispatch(_event())
    assert len(first) == 2

    # The endpoint comes back; the retry must reach it and only it.
    with requests_mock.Mocker() as m:
        m.post(good.url, status_code=200)
        m.post(flaky.url, status_code=200)
        retried = dispatcher.retry_failed(_event(), first)

    assert [r["url"] for r in retried] == [flaky.url]
    assert retried[0]["success"] is True
    assert good.url not in [r.url for r in m.request_history]
    assert all_delivered(retried)


def test_retry_failed_is_a_noop_when_nothing_is_owed():
    results = [
        WebhookDispatcher._result(
            url="http://localhost:8888/ok",
            event="task_created",
            event_id="evt_1",
            outcome=OUTCOME_SUCCESS,
        )
    ]
    dispatcher = WebhookDispatcher(
        webhooks=[WebhookConfig(url="http://localhost:8888/ok")]
    )
    assert dispatcher.retry_failed(_event(), results) == []
    assert retryable_results(results) == []
    assert all_delivered(results) is True


def test_permanent_4xx_records_an_explicit_outcome():
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=404, text="gone")
        results = WebhookDispatcher(webhooks=[wh], max_retries=3).dispatch(_event())

    assert results[0]["outcome"] == OUTCOME_PERMANENT_FAILURE
    assert results[0]["retryable"] is False
    assert results[0]["attempts"] == 1


def test_transient_failure_is_marked_retryable():
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    clock = _FakeClock()
    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=503, text="unavailable")
        results = WebhookDispatcher(
            webhooks=[wh], max_retries=2, clock=clock, sleep=clock.sleep
        ).dispatch(_event())

    assert results[0]["outcome"] == OUTCOME_TRANSIENT_FAILURE
    assert results[0]["retryable"] is True
    assert results[0]["attempts"] == 2


def test_result_exposes_a_redacted_url_display():
    wh = WebhookConfig(url=WECOM_URL, secret=FAKE_SECRET)
    with requests_mock.Mocker() as m:
        m.post(WECOM_URL, status_code=200)
        results = WebhookDispatcher(webhooks=[wh]).dispatch(_event())

    assert results[0]["url_display"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***"
    )
    assert "693a91f6" not in results[0]["url_display"]


# ── An empty secret must not silently mean "unsigned" ────────────────────


def test_missing_secret_is_reported_not_silent(caplog):
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret="")
    dispatcher = WebhookDispatcher(webhooks=[wh])

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        with caplog.at_level(logging.DEBUG, logger="mb_cli.daemon.webhook"):
            results = dispatcher.dispatch(_event())

    assert results[0]["success"] is True
    assert results[0]["signed"] is False, "the unsigned state must be explicit"
    assert "X-MB-Signature" not in m.last_request.headers
    unsigned = [r for r in caplog.records if "UNSIGNED" in r.getMessage()]
    assert unsigned, "an unsigned webhook must be complained about"
    assert unsigned[0].levelno >= logging.ERROR, "the complaint must be loud"
    assert "UNSIGNED" in caplog.text


def test_missing_secret_is_warned_about_once_per_endpoint(caplog):
    """A 30s poll loop must not emit the same error on every event."""
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=None)
    dispatcher = WebhookDispatcher(webhooks=[wh])

    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        with caplog.at_level(logging.DEBUG, logger="mb_cli.daemon.webhook"):
            for _ in range(3):
                results = dispatcher.dispatch(_event())

    warnings = [r for r in caplog.records if "UNSIGNED" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert all(r["signed"] is False for r in results)


def test_signed_delivery_reports_signed():
    wh = WebhookConfig(url="http://localhost:8888/webhook", secret=FAKE_SECRET)
    with requests_mock.Mocker() as m:
        m.post(wh.url, status_code=200)
        results = WebhookDispatcher(webhooks=[wh]).dispatch(_event())

    assert results[0]["signed"] is True
    assert results[0]["outcome"] == OUTCOME_SUCCESS
    assert m.last_request.headers["X-MB-Signature"].startswith("sha256=")


# ── test_ping must validate the URL like a real dispatch ─────────────────


@pytest.mark.parametrize(
    "bad_url",
    ["file:///etc/passwd", "ftp://example.test/hook", "not-a-url", "", "http://"],
)
def test_test_ping_rejects_invalid_url_without_touching_the_network(bad_url):
    dispatcher = WebhookDispatcher()

    with requests_mock.Mocker() as m:
        results = dispatcher.test_ping(bad_url, secret=FAKE_SECRET)
        assert m.call_count == 0, f"{bad_url!r} reached the network"

    assert results["success"] is False
    assert results["outcome"] == OUTCOME_PERMANENT_FAILURE
    assert results["retryable"] is False
    assert results["attempts"] == 0
    assert results["error"].startswith("invalid_webhook_url:")
    assert results["status_code"] is None
    assert results["signed"] is True


def test_test_ping_accepts_a_valid_url():
    url = "http://localhost:8888/test-hook"
    with requests_mock.Mocker() as m:
        m.post(url, status_code=200)
        res = WebhookDispatcher().test_ping(url)

    assert res["success"] is True
    assert res["status_code"] == 200
    assert res["event"] == "test_ping"
    assert res["signed"] is False, "no secret was supplied"


# ── Pre-existing contract ────────────────────────────────────────────────


def test_webhook_dispatch_success():
    wh_url = "http://localhost:8888/webhook"
    wh = WebhookConfig(url=wh_url, secret="test-secret")
    dispatcher = WebhookDispatcher(webhooks=[wh])

    event = MBEvent(
        event="deadline_approaching",
        data={"task_id": "999", "title": "Final Project"},
    )

    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        results = dispatcher.dispatch(event)
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["status_code"] == 200

        # Verify signature header
        req = m.last_request
        assert req.headers.get("X-MB-Event") == "deadline_approaching"
        assert req.headers.get("X-MB-Signature", "").startswith("sha256=")


def test_webhook_test_ping():
    wh_url = "http://localhost:8888/test-hook"
    dispatcher = WebhookDispatcher()

    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        res = dispatcher.test_ping(wh_url)
        assert res["success"] is True
        assert res["status_code"] == 200
