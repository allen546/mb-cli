"""Tests for the webhook dispatcher — signed material.

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

from mb_cli.daemon import webhook as webhook_module
from mb_cli.daemon.events import MBEvent, WebhookConfig
from mb_cli.daemon.webhook import WebhookDispatcher, signed_material

FAKE_SECRET = "test-secret-not-real"

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

    import requests_mock

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
    import requests_mock

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
    import requests_mock

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
    import requests_mock

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
    import requests_mock

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


# ── Pre-existing contract ────────────────────────────────────────────────


def test_webhook_dispatch_success():
    import requests_mock

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
    import requests_mock

    wh_url = "http://localhost:8888/test-hook"
    dispatcher = WebhookDispatcher()

    with requests_mock.Mocker() as m:
        m.post(wh_url, status_code=200)
        res = dispatcher.test_ping(wh_url)
        assert res["success"] is True
        assert res["status_code"] == 200
