"""Regression tests for the MNN Hub client's transport behaviour.

Two defects are pinned here, both measured against the live hub
(``mnn-hub.prod.faria.cn``) and recorded in ``docs/realtime-transport-findings.md``:

1. ``_jitter()`` slept a uniform 1-3 s inside ``stats()`` and ``list()`` — the
   two cheap reads — and inside none of the mutating methods. The transport
   answers in ~75 ms, so the sleep cost 26x what it guarded, and there are no
   ``X-RateLimit-*`` / ``Retry-After`` headers to justify it.
2. Reads were unconditional, so every poll re-downloaded ~90 KB. The hub honours
   ``If-None-Match`` on both read endpoints and answers 304 with 0 bytes.

Everything here talks to ``requests_mock``; nothing touches the filesystem or
the real hub.
"""

from __future__ import annotations

import time

import pytest
import requests_mock

from tahuti.notifications import MNNHubClient

ENDPOINT = "https://mnn-hub.prod.faria.com"
STATS_URL = f"{ENDPOINT}/api/frontend/v2/notifications/stats"
LIST_URL = f"{ENDPOINT}/api/frontend/v2/notifications"

STATS_BODY = {"stats": {"unread_messages": 3, "unread_announcements": 1}}
STATS_BODY_V2 = {"stats": {"unread_messages": 7, "unread_announcements": 2}}
ETAG_V1 = 'W/"14e0955c00000000000000000000001"'
ETAG_V2 = 'W/"14e0955c00000000000000000000002"'

LIST_BODY = {
    "items": [{"id": 1, "title": "One"}, {"id": 2, "title": "Two"}],
    "meta": {"page": 1, "total": 2},
}


@pytest.fixture()
def hub():
    return MNNHubClient(ENDPOINT, "test_token")


# ── Defect 1: no sleep on the read path ─────────────────────────────────


def _explode(_seconds: float) -> None:
    raise AssertionError("the MNN Hub read path must not sleep")


@pytest.mark.parametrize("call", ["stats", "list"])
def test_read_path_does_not_sleep(hub, monkeypatch, call):
    """``stats()``/``list()`` used to sleep 1-3 s each via ``_jitter()``.

    Pacing belongs to the daemon scheduler (``DaemonConfig.poll_jitter_seconds``),
    not inside the transport: a poll loop that sleeps inside its own client
    cannot poll tightly. See the module docstring in ``tahuti/notifications.py``.
    """
    monkeypatch.setattr(time, "sleep", _explode)

    with requests_mock.Mocker() as m:
        m.get(STATS_URL, json=STATS_BODY)
        m.get(LIST_URL, json=LIST_BODY)
        getattr(hub, call)()


def test_mutating_methods_do_not_sleep_either(hub, monkeypatch):
    """Writes were never jittered; keep them that way after the refactor."""
    monkeypatch.setattr(time, "sleep", _explode)

    with requests_mock.Mocker() as m:
        m.put(f"{LIST_URL}/1234/read", status_code=204)
        m.put(f"{LIST_URL}/1234/unread", status_code=204)
        m.put(f"{LIST_URL}/mark_as_read", status_code=204)
        m.put(f"{LIST_URL}/1234/star", status_code=204)
        m.put(f"{LIST_URL}/1234/unstar", status_code=204)
        assert hub.mark_read(1234) is True
        assert hub.mark_unread(1234) is True
        assert hub.mark_all_read() is True
        assert hub.star(1234) is True
        assert hub.unstar(1234) is True


# ── Defect 2: conditional requests ──────────────────────────────────────


def test_first_stats_call_sends_no_if_none_match_and_stores_etag(hub):
    with requests_mock.Mocker() as m:
        m.get(STATS_URL, json=STATS_BODY, headers={"ETag": ETAG_V1})
        assert hub.stats() == STATS_BODY["stats"]
        assert "If-None-Match" not in m.request_history[0].headers
        assert hub.etags[("/notifications/stats", ())] == ETAG_V1


def test_second_stats_call_replays_the_etag(hub):
    with requests_mock.Mocker() as m:
        m.get(
            STATS_URL,
            [
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
                {"status_code": 304},
            ],
        )
        hub.stats()
        hub.stats()
        assert m.request_history[1].headers["If-None-Match"] == ETAG_V1


def test_304_does_not_raise_and_returns_the_cached_body(hub):
    """Before the fix a 304 hit ``raise_for_status()`` and blew up the poll."""
    with requests_mock.Mocker() as m:
        m.get(
            STATS_URL,
            [
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
                {"status_code": 304},
            ],
        )
        first = hub.stats()
        second = hub.stats()
    assert first == STATS_BODY["stats"]
    assert second == first


def test_changed_etag_refreshes_the_body(hub):
    with requests_mock.Mocker() as m:
        m.get(
            STATS_URL,
            [
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
                {"status_code": 304},
                {"json": STATS_BODY_V2, "headers": {"ETag": ETAG_V2}},
                {"status_code": 304},
            ],
        )
        assert hub.stats() == STATS_BODY["stats"]
        assert hub.stats() == STATS_BODY["stats"]          # cached, 304
        assert hub.stats() == STATS_BODY_V2["stats"]       # refreshed, 200
        assert hub.stats() == STATS_BODY_V2["stats"]       # cached, 304
        assert m.request_history[2].headers["If-None-Match"] == ETAG_V1
        assert m.request_history[3].headers["If-None-Match"] == ETAG_V2


def test_list_is_conditional_too(hub):
    with requests_mock.Mocker() as m:
        m.get(
            LIST_URL,
            [
                {"json": LIST_BODY, "headers": {"ETag": ETAG_V1}},
                {"status_code": 304},
            ],
        )
        first = hub.list(page=1, per_page=20, filter_="all")
        second = hub.list(page=1, per_page=20, filter_="all")
        assert first == {"items": LIST_BODY["items"], "meta": LIST_BODY["meta"]}
        assert second == first
        assert m.request_history[1].headers["If-None-Match"] == ETAG_V1


def test_etag_is_scoped_per_query_string(hub):
    """``list()`` is called with different ``per_page``/``filter`` values by
    different callers; one shared Etag would cross-contaminate them."""
    with requests_mock.Mocker() as m:
        m.get(
            LIST_URL,
            [
                {"json": LIST_BODY, "headers": {"ETag": ETAG_V1}},
                {"json": LIST_BODY, "headers": {"ETag": ETAG_V2}},
            ],
        )
        hub.list(page=1, per_page=20, filter_="all")
        hub.list(page=1, per_page=10, filter_="unread")
        assert "If-None-Match" not in m.request_history[1].headers


def test_mutation_invalidates_the_cached_etag(hub):
    """A write changes what both reads return, so the hub would keep answering
    304 for a stale Etag and the client would replay an out-of-date count."""
    with requests_mock.Mocker() as m:
        m.get(STATS_URL, [
            {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
            {"status_code": 304},
            {"json": STATS_BODY_V2, "headers": {"ETag": ETAG_V2}},
        ])
        m.put(f"{LIST_URL}/1/read", status_code=200)

        hub.stats()
        hub.stats()
        assert m.request_history[1].headers["If-None-Match"] == ETAG_V1

        assert hub.mark_read(1) is True
        assert hub.stats() == STATS_BODY_V2["stats"]
        assert "If-None-Match" not in m.request_history[3].headers


def test_failed_mutation_keeps_the_cached_etag(hub):
    """Nothing changed server-side, so the cached Etag is still valid."""
    with requests_mock.Mocker() as m:
        m.get(STATS_URL, [
            {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
            {"status_code": 304},
        ])
        m.put(f"{LIST_URL}/1/read", status_code=500)

        hub.stats()
        hub.stats()
        assert hub.mark_read(1) is False
        hub.stats()
        assert m.request_history[3].headers["If-None-Match"] == ETAG_V1


def test_304_without_a_cached_body_recovers_unconditionally(hub):
    """An intermediary must not be able to wedge the client on an empty reply."""
    with requests_mock.Mocker() as m:
        m.get(
            STATS_URL,
            [
                {"status_code": 304},
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
            ],
        )
        assert hub.stats() == STATS_BODY["stats"]
        assert "If-None-Match" not in m.request_history[1].headers


def test_conditional_requests_can_be_disabled(hub):
    hub = MNNHubClient(ENDPOINT, "test_token", conditional=False)
    with requests_mock.Mocker() as m:
        m.get(
            STATS_URL,
            [
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
            ],
        )
        hub.stats()
        hub.stats()
        assert "If-None-Match" not in m.request_history[0].headers
        assert "If-None-Match" not in m.request_history[1].headers


def test_no_etag_header_means_no_conditional_request(hub):
    """Some intermediaries strip Etags; the client must still work then."""
    with requests_mock.Mocker() as m:
        m.get(STATS_URL, [{"json": STATS_BODY}, {"json": STATS_BODY}])
        hub.stats()
        hub.stats()
        assert hub.etags == {}
        assert "If-None-Match" not in m.request_history[1].headers
