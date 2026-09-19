"""Characterization and regression tests for ``MNNHubProvider``.

``normalize_notification`` is the most convoluted code in the notification path:
it re-parses HTML the hub already rendered, maps event names, and then runs a
four-branch "is this title generic?" heuristic. Nothing here rewrites it — the
first block pins its current behaviour on payloads shaped like the ones the hub
actually sends (samples taken from ``extras/mb-notifier/test_bark_webhook.py``
and ``tests/test_daemon_provider.py``), so a future refactor can tell a
deliberate change from an accident.

The second block covers the ``notif_id`` defect: ``MNNHubProvider`` put
``item["id"]`` straight into ``event_id`` and ``data["notification_id"]`` without
validating it, and ``DaemonService.run_check_cycle`` calls ``int()`` on that
value at three places inside one ``try`` that wraps the whole event loop
(``src/tahuti/daemon/service.py:217``, ``:308``, ``:356``).

Everything is mocked; nothing touches the network or the filesystem outside a
``tmp_path`` state file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import requests_mock
from bs4 import BeautifulSoup

from tahuti.client import ManageBacClient
from tahuti.daemon.events import MBEvent
from tahuti.daemon.provider import MNNHubProvider
from tahuti.daemon.service import DaemonService
from tahuti.daemon.state import DaemonStateManager


HUB = "https://mnn-hub.prod.faria.cn"
STATS_URL = f"{HUB}/api/frontend/v2/notifications/stats"
LIST_URL = f"{HUB}/api/frontend/v2/notifications"
STATS_BODY = {"stats": {"unread_messages": 3}}
ETAG_V1 = 'W/"14e0955c00000000000000000000001"'
LIST_BODY = {"items": [{"id": 1, "title": "One"}], "meta": {"page": 1, "total": 1}}


@pytest.fixture()
def provider():
    return MNNHubProvider(MagicMock())


# Payloads shaped like real hub responses. `body` is the rendered HTML the hub
# ships; `body_preview` is the plain-text version it ships alongside.
TASK_WITH_LINK = {
    "id": 244677168,
    "title": "Updated Task",
    "event_name": "task_updated",
    "created_at": "2026-09-02T07:44:54.316Z",
    "body": (
        '<p>Updated <a href="https://school.managebac.cn/student/classes/1000001'
        '/core_tasks/1000015">Task</a></p>'
        "<p>When: September 10, 2026 at 9:10 AM</p>"
    ),
    "body_preview": "Updated Task NAME LIST",
    "sender": {"name": "Teacher Name"},
    "origin": {"name": "Physics Class"},
}

GENERIC_TITLE_STRONG_BODY = {
    "id": 246223933,
    "title": "Updated Task",
    "event_name": "task_updated",
    "created_at": "2026-09-11T02:00:00.000Z",
    "body": (
        '<p style="margin:0 0 10px"><strong style="font-weight:600">A. Teacher</strong>'
        ' has just updated the Task <strong style="font-weight:600">Materials Check'
        "</strong> in "
        '<a href="https://demo-school.managebac.cn/student/classes/1000010/calendar">'
        "AP English Language Arts I (Hons) - Group 2 (Grade 10)</a>.</p>"
        '<p style="margin:0 0 10px"> <strong style="font-weight:600">When:</strong>'
        " September 11, 2026 at 12:10 PM </p>"
        '<p style="margin:0 0 10px"><a href="https://demo-school.managebac.cn/student'
        '/classes/1000001/core_tasks/1000017">View full details</a></p>'
    ),
    "body_preview": (
        "A. Teacher has just updated the Task Materials Check in AP English "
        "Language Arts I (Hons) - Group 2 (Grade 10). When: September 11, 2026 at "
        "12:10 PM View full details"
    ),
    "sender": {"name": "A. Teacher"},
    "origin": {"name": "AP English Language Arts I (Hons) - Group 2 (Grade 10)"},
}

TITLE_EQUALS_CLASS_NAME = {
    "id": 246300001,
    "title": "AP AP—Calculus BC (Grade 10) Yellow",
    "event_name": "new_task",
    "created_at": "2026-09-13T02:00:00.000Z",
    "body": (
        '<p><strong>Hongjing (Sarah) Shi</strong> has just added a new Task '
        '<strong>Unit 1.3 Library of functions</strong> in '
        '<a href="https://demo-school.managebac.cn/student/classes/1000001/calendar">'
        "AP AP—Calculus BC (Grade 10) Yellow</a>.</p>"
        "<p><strong>When:</strong> September 13, 2026 at 11:55 PM</p>"
    ),
    "body_preview": (
        "Hongjing (Sarah) Shi has just added a new Task Unit 1.3 Library of "
        "functions in AP AP—Calculus BC (Grade 10) Yellow. When: September 13, "
        "2026 at 11:55 PM View full details"
    ),
    "sender": {"name": "Hongjing (Sarah) Shi"},
    "origin": {"name": "AP AP—Calculus BC (Grade 10) Yellow"},
}

PREVIEW_ONLY = {
    "id": 246300002,
    "title": "New Task",
    "event_name": "task_created",
    "created_at": "2026-09-11T10:10:00",
    "body": "",
    "body_preview": "Teacher has added a new Task 语文早读小测1 in Chinese Language Arts I",
    "sender": {"name": "Teacher"},
    "origin": {"name": "Chinese Language Arts I"},
}

ANNOUNCEMENT = {
    "id": 246300003,
    "title": "School Closure Monday",
    "event_name": "announcement_created",
    "created_at": "2026-09-12T01:00:00",
    "body": "<p>School closed on Monday.</p>",
    "body_preview": "School closed on Monday.",
    "sender": {"name": "Admin"},
    "origin": {"name": "School"},
}


# ── Characterization: what the current normalizer produces ──────────────


class TestNormalizeNotificationCharacterization:
    def test_core_tasks_link_yields_ids_and_due_date(self, provider):
        event = provider.normalize_notification(dict(TASK_WITH_LINK))
        assert event.event == "task_updated"
        assert event.event_id == "notif_244677168"
        assert event.timestamp == "2026-09-02T07:44:54.316Z"
        assert event.data["task_id"] == 1000015
        assert event.data["class_id"] == 1000001
        assert event.data["due_date"] == "September 10, 2026 at 9:10 AM"
        assert (
            event.data["url"]
            == "https://school.managebac.cn/student/classes/1000001/core_tasks/1000015"
        )
        # A specific title ("Updated Task" is generic) is left alone here: the
        # heuristic only reaches into the body when the title looks generic.
        assert event.data["task_title"] == "Updated Task"

    def test_generic_title_is_replaced_from_strong_tag(self, provider):
        event = provider.normalize_notification(dict(GENERIC_TITLE_STRONG_BODY))
        assert event.data["task_title"] == "Materials Check"
        # The calendar link gives a class but no task, and the core_tasks link
        # later in the same body wins for both.
        assert event.data["class_id"] == 1000001
        assert event.data["task_id"] == 1000017
        assert event.data["due_date"] == "September 11, 2026 at 12:10 PM"

    def test_title_matching_class_name_falls_back_to_body(self, provider):
        event = provider.normalize_notification(dict(TITLE_EQUALS_CLASS_NAME))
        assert event.event == "task_created"
        assert event.data["class_name"] == "AP AP—Calculus BC (Grade 10) Yellow"
        assert event.data["task_title"] == "Unit 1.3 Library of functions"

    def test_preview_is_used_when_body_html_has_no_strong_tag(self, provider):
        event = provider.normalize_notification(dict(PREVIEW_ONLY))
        assert event.data["task_title"] == "语文早读小测1"

    def test_announcement_carries_no_task_identifiers(self, provider):
        event = provider.normalize_notification(dict(ANNOUNCEMENT))
        assert event.event == "announcement_created"
        assert "task_id" not in event.data
        assert "class_id" not in event.data
        assert "url" not in event.data
        assert event.validate() is True

    def test_non_task_events_are_not_forced_into_the_task_schema(self, provider):
        """Only TASK_EVENT_TYPES must carry STANDARD_TASK_FIELDS."""
        event = provider.normalize_notification(dict(ANNOUNCEMENT))
        assert event.event not in ("task_created", "task_updated", "task_graded")

    def test_every_normalized_event_survives_the_envelope_contract(self, provider):
        for item in (
            TASK_WITH_LINK,
            GENERIC_TITLE_STRONG_BODY,
            TITLE_EQUALS_CLASS_NAME,
            PREVIEW_ONLY,
            ANNOUNCEMENT,
        ):
            event = provider.normalize_notification(dict(item))
            assert isinstance(event.event, str) and event.event
            assert isinstance(event.event_id, str) and event.event_id
            assert isinstance(event.timestamp, str) and event.timestamp
            assert isinstance(event.data, dict)

    def test_raw_fields_are_preserved_alongside_the_derived_ones(self, provider):
        event = provider.normalize_notification(dict(TASK_WITH_LINK))
        assert event.data["title"] == "Updated Task"
        assert event.data["raw_event_name"] == "task_updated"
        assert event.data["sender"] == {"name": "Teacher Name"}
        assert event.data["origin"] == {"name": "Physics Class"}
        assert event.data["notification_id"] == 244677168

    @pytest.mark.parametrize(
        ("raw_event", "expected"),
        [
            ("task_created", "task_created"),
            ("new_task", "task_created"),
            ("task_updated", "task_updated"),
            ("updated_task", "task_updated"),
            ("assignment_graded", "assignment_graded"),
            ("grade_posted", "assignment_graded"),
            ("new_file_uploaded", "file_uploaded"),
            ("file_uploaded", "file_uploaded"),
            ("announcement_created", "announcement_created"),
            ("new_announcement", "announcement_created"),
            ("message_created", "announcement_created"),
            ("something_unmapped", "notification"),
        ],
    )
    def test_event_name_mapping(self, raw_event, expected):
        assert MNNHubProvider._map_event_type(raw_event) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("New Task for you", "task_created"),
            ("Task added to Physics", "task_created"),
            ("Task created in Math", "task_created"),
            ("Updated Task: Lab Report", "task_updated"),
            ("Task updated", "task_updated"),
            ("New file uploaded", "file_uploaded"),
            ("Grade posted for Homework 3", "assignment_graded"),
            ("Homework 3 graded", "assignment_graded"),
            ("Announcement from the office", "announcement_created"),
            ("Something else entirely", "notification"),
        ],
    )
    def test_event_type_falls_back_to_title_keywords(self, title, expected):
        assert MNNHubProvider._map_event_type("unmapped_event", title) == expected

    def test_poll_events_normalizes_every_raw_item(self, provider):
        provider.fetch_raw_notifications = lambda per_page=20: [  # type: ignore[method-assign]
            dict(TASK_WITH_LINK),
            dict(ANNOUNCEMENT),
        ]
        events = provider.poll_events()
        assert [e.event for e in events] == ["task_updated", "announcement_created"]


# ── Defect: notif_id is used unvalidated ────────────────────────────────


class TestNotificationIdValidation:
    def test_missing_id_is_not_stringified_into_an_event_id(self, provider):
        """``f"notif_{notif_id}"`` with ``notif_id is None`` yields the literal
        string ``"notif_None"``, which is then sent to webhooks as the
        ``event_id`` of a real event."""
        item = dict(PREVIEW_ONLY)
        item.pop("id")
        event = provider.normalize_notification(item)
        assert event.event_id != "notif_None"
        assert event.data["notification_id"] is None

    def test_idless_events_get_distinct_event_ids(self, provider):
        """Two id-less notifications must not collide on one event_id."""
        item = dict(PREVIEW_ONLY)
        item.pop("id")
        first = provider.normalize_notification(dict(item))
        second = provider.normalize_notification(dict(item))
        assert first.event_id != second.event_id
        assert first.event_id.startswith("evt_")

    def test_numeric_string_id_is_normalized_to_int(self, provider):
        item = dict(PREVIEW_ONLY)
        item["id"] = "246300004"
        event = provider.normalize_notification(item)
        assert event.data["notification_id"] == 246300004
        assert event.event_id == "notif_246300004"

    def test_non_numeric_id_is_dropped_not_forwarded(self, provider):
        item = dict(PREVIEW_ONLY)
        item["id"] = "abc-123"
        event = provider.normalize_notification(item)
        assert event.data["notification_id"] is None
        assert event.event_id != "notif_abc-123"

    @pytest.mark.parametrize("bad_id", [None, "", "abc-123", "12.5", {"id": 1}, [7]])
    def test_no_normalized_event_feeds_a_raising_int(self, provider, bad_id):
        """Mirrors ``DaemonService.run_check_cycle``: it calls ``int()`` on
        ``data["notification_id"]`` guarded only by ``if notif_id``. Any value
        that is truthy but not an int raises ValueError there.
        """
        for item in (dict(PREVIEW_ONLY), dict(ANNOUNCEMENT)):
            item["id"] = bad_id
            event = provider.normalize_notification(item)
            notif_id = event.data.get("notification_id")
            if notif_id:
                int(notif_id)  # must not raise


def _run_cycle(tmp_path: Path, events: list[MBEvent]) -> dict:
    """Run one real ``DaemonService.run_check_cycle`` over ``events``.

    Uses the real service on purpose: the ``int(notif_id)`` call that a
    non-numeric id breaks lives there, and the ``try`` around the event loop
    swallows the resulting ValueError, so a provider-level test cannot see the
    damage it does.
    """
    from tahuti.daemon.events import DaemonConfig
    from tahuti.daemon.provider import AbstractNotificationProvider

    class StubProvider(AbstractNotificationProvider):
        def __init__(self):
            self.events = list(events)

        def start(self) -> None: ...
        def stop(self) -> None: ...

        def poll_events(self) -> list[MBEvent]:
            out, self.events = self.events, []
            return out

        def refresh_auth(self) -> bool:
            return True

    client = MagicMock()
    client.get_tasks_by_view.return_value = []
    # The service enriches task events via StealthTaskCrawler, which re-parses
    # whatever `client._get` returns. Give it a real page so enrichment is a
    # no-op parse rather than a MagicMock that blows up inside `re.search`.
    client._get.return_value = BeautifulSoup(
        "<html><body><h3 class='title'>Lab Report 1</h3>"
        "<a href='/student/classes/1000001'>Physics Class</a>"
        "<p>Due: September 10, 2026 at 9:10 AM</p></body></html>",
        "html.parser",
    )
    client.get_submissions.return_value = []
    state = DaemonStateManager(tmp_path / "state.json")
    service = DaemonService(
        client=client,
        config=DaemonConfig(),
        state_manager=state,
        provider=StubProvider(),
    )
    service.dispatcher.dispatch = MagicMock(return_value=[{"success": True}])
    return service.run_check_cycle()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "defect in a file this worktree does not own: service.py:217 calls "
        "int(notif_id) inside the try that wraps the whole event loop, so one "
        "non-numeric notification_id aborts the cycle and drops every event "
        "queued behind it. MNNHubProvider now normalizes its own ids, but any "
        "other provider (e.g. MobilePushProvider) can still feed this path. "
        "Route to whoever owns daemon/service.py."
    ),
)
def test_a_bad_notification_id_does_not_kill_the_rest_of_the_poll_cycle(tmp_path):
    """``run_check_cycle`` wraps the whole event loop in one bare ``except``
    (``service.py:358``). One notification whose ``id`` is not an int therefore
    aborts the cycle and every notification queued behind it is dropped
    silently — the daemon never re-polls them, because nothing marked them
    processed.
    """
    bad = MBEvent(
        event="announcement_created",
        event_id="notif_abc-123",
        timestamp="2026-09-12T01:00:00",
        data={"notification_id": "abc-123", "title": "Bad id"},
    )
    good = MBEvent(
        event="announcement_created",
        event_id="notif_246300003",
        timestamp="2026-09-12T01:00:01",
        data={"notification_id": 246300003, "title": "Good id"},
    )
    result = _run_cycle(tmp_path, [bad, good])
    assert result["new_notifications"] == 2


def test_poll_cycle_marks_a_normalized_batch_processed(tmp_path):
    """The provider's normalization is what the service loop relies on, end to
    end: a batch of real-shaped payloads must all be dispatched and recorded."""
    provider = MNNHubProvider(MagicMock())
    events = [
        provider.normalize_notification(dict(TASK_WITH_LINK)),
        provider.normalize_notification(dict(ANNOUNCEMENT)),
    ]
    result = _run_cycle(tmp_path, events)
    assert result["new_notifications"] == 2
    state = DaemonStateManager(tmp_path / "state.json")
    assert state.is_notification_processed(244677168)
    assert state.is_notification_processed(246300003)


# ── _ensure_hub / _acquire_token ────────────────────────────────────────


def _hub_client():
    client = MagicMock()
    client.session.verify = True
    client.domain = "managebac.cn"
    client.get_notification_token.return_value = (
        "https://mnn-hub.prod.faria.cn",
        "jwt_token",
    )
    # `_ensure_hub` routes the scraped endpoint through the real validator, so a
    # MagicMock here would stub the guard out and hand its return value straight
    # to requests as a URL. Binding the production method keeps these tests
    # exercising the same allowlist the daemon actually runs.
    client._validated_hub_endpoint = ManageBacClient._validated_hub_endpoint.__get__(
        client
    )
    return client


class TestEnsureHub:
    def test_token_is_acquired_once_and_reused(self):
        client = _hub_client()
        provider = MNNHubProvider(client)
        assert provider._ensure_hub() is provider._ensure_hub()
        client.get_notification_token.assert_called_once_with(bypass_cache=True)

    def test_stop_forces_a_fresh_hub_on_the_next_use(self):
        client = _hub_client()
        provider = MNNHubProvider(client)
        first = provider._ensure_hub()
        provider.stop()
        assert provider._ensure_hub() is not first
        assert client.get_notification_token.call_count == 2

    def test_missing_endpoint_falls_back_to_the_domain_hub(self):
        client = _hub_client()
        client.get_notification_token.return_value = ("", "jwt_token")
        provider = MNNHubProvider(client)
        assert provider._ensure_hub().base.startswith("https://mnn-hub.prod.faria.cn")
        assert provider.hub_endpoint == "https://mnn-hub.prod.faria.cn"

    @pytest.mark.parametrize(
        "scraped",
        [
            "https://mnn-hub.prod.faria.cn@evil.test",  # userinfo spoof
            "http://mnn-hub.prod.faria.cn",  # cleartext downgrade
            "https://evil.test/collect",  # foreign host
            "wss://evil.test",  # non-http scheme
        ],
    )
    def test_hostile_scraped_endpoint_never_receives_the_jwt(self, scraped):
        """The daemon polls unattended, so it must not trust the scraped host.

        `get_notification_token()` returns `data-mnn-hub-endpoint` verbatim from
        scraped HTML and the token is sent as `Authorization: Bearer <jwt>`.
        `_ensure_hub` used to pass that value straight to `MNNHubClient`, letting
        a poisoned page or TLS-stripping MITM choose where the JWT lands.
        """
        client = _hub_client()
        client.get_notification_token.return_value = (scraped, "jwt_token")
        provider = MNNHubProvider(client)

        hub = provider._ensure_hub()

        assert "evil.test" not in provider.hub_endpoint, (
            f"daemon sent the hub JWT to attacker-chosen host {provider.hub_endpoint!r}"
        )
        assert provider.hub_endpoint.startswith("https://"), (
            f"token would travel in cleartext: {provider.hub_endpoint!r}"
        )
        assert hub.base.startswith("https://mnn-hub.prod.faria.cn")

    def test_unrelated_token_error_propagates_without_relogin(self):
        client = _hub_client()
        client.get_notification_token.side_effect = RuntimeError("network is down")
        refresh = MagicMock(return_value=True)
        provider = MNNHubProvider(client, auth_refresh_fn=refresh)
        with pytest.raises(RuntimeError, match="network is down"):
            provider._ensure_hub()
        refresh.assert_not_called()

    def test_expired_session_relogin_failure_reraises_the_original_error(self):
        client = _hub_client()
        client.get_notification_token.side_effect = RuntimeError(
            "Session expired or invalid — redirected to login"
        )
        refresh = MagicMock(return_value=False)
        provider = MNNHubProvider(client, auth_refresh_fn=refresh)
        with pytest.raises(RuntimeError, match="Session expired"):
            provider._ensure_hub()
        refresh.assert_called_once_with()
        client.get_notification_token.assert_called_once_with(bypass_cache=True)

    def test_expired_session_with_no_refresh_callback_reraises(self):
        client = _hub_client()
        client.get_notification_token.side_effect = RuntimeError(
            "Session expired or invalid — redirected to login"
        )
        provider = MNNHubProvider(client)
        with pytest.raises(RuntimeError, match="Session expired"):
            provider._ensure_hub()


class TestHubReadsRetryOnAuthFailure:
    """The provider retries a read once after a 401/403, via the hub client."""

    @pytest.fixture()
    def wired(self):
        client = _hub_client()
        provider = MNNHubProvider(client, auth_refresh_fn=MagicMock(return_value=True))
        provider.start()
        return provider

    def test_stats_refreshes_the_token_on_401(self, wired):
        with requests_mock.Mocker() as m:
            m.get(STATS_URL, [
                {"status_code": 401, "json": {}},
                {"json": STATS_BODY, "headers": {"ETag": ETAG_V1}},
            ])
            assert wired.get_stats() == STATS_BODY["stats"]
        wired.auth_refresh_fn.assert_called_once_with()

    def test_stats_reraises_after_a_successful_refresh_still_fails(self, wired):
        with requests_mock.Mocker() as m:
            m.get(STATS_URL, status_code=403, json={})
            with pytest.raises(requests.HTTPError):
                wired.get_stats()

    def test_stats_reraises_a_non_auth_error_untouched(self, wired):
        with requests_mock.Mocker() as m:
            m.get(STATS_URL, status_code=500, json={})
            with pytest.raises(requests.HTTPError):
                wired.get_stats()
        wired.auth_refresh_fn.assert_not_called()

    def test_fetch_raw_notifications_refreshes_on_403(self, wired):
        with requests_mock.Mocker() as m:
            m.get(LIST_URL, [
                {"status_code": 403, "json": {}},
                {"json": LIST_BODY, "headers": {"ETag": ETAG_V1}},
            ])
            assert wired.fetch_raw_notifications(per_page=20) == LIST_BODY["items"]

    def test_fetch_raw_notifications_reraises_other_errors(self, wired):
        with requests_mock.Mocker() as m:
            m.get(LIST_URL, status_code=502, json={})
            with pytest.raises(requests.HTTPError):
                wired.fetch_raw_notifications(per_page=20)


class TestRefreshAuth:
    def test_returns_true_and_rebuilds_the_hub(self):
        client = _hub_client()
        refresh = MagicMock(return_value=True)
        provider = MNNHubProvider(client, auth_refresh_fn=refresh)
        provider.start()
        assert provider.refresh_auth() is True
        refresh.assert_called_once_with()
        assert client.get_notification_token.call_count == 2

    def test_returns_false_when_the_token_call_still_fails(self):
        client = _hub_client()
        client.get_notification_token.side_effect = [
            ("https://mnn-hub.prod.faria.cn", "jwt_token"),
            RuntimeError("still expired"),
        ]
        provider = MNNHubProvider(client, auth_refresh_fn=MagicMock(return_value=True))
        provider.start()
        assert provider.refresh_auth() is False
