"""Regression tests for the confirmed scraping/transport defects in client.py.

Every test in this module fails against the pre-fix implementation and passes
after it.  They are grouped by the defect number they prove, and each class
docstring states the property being protected rather than the bug, so the test
survives the bug being forgotten.

No test here talks to ManageBac: HTTP is served by ``requests_mock``.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
import requests_mock as rm

from mb_cli.cache import ResponseCache
from mb_cli.client import ManageBacClient, parse_due_date
from mb_cli.exceptions import CommandError

BASE = "https://myschool.managebac.cn"

LOGIN_PAGE = """<html><head><title>Sign in | ManageBac</title></head><body>
<form action="/sessions" method="post" class="sign-in-form">
  <input name="authenticity_token" value="tok">
  <input type="email" name="login" id="login">
  <input type="password" name="password" id="password">
</form></body></html>"""

TASKS_PAGE = """<html><head><title>Tasks</title></head><body>
  <a href="/student/profile">Test Student — ManageBac</a>
  <div class="f-task-tile">
    <a class="f-tile__title-link" href="/student/classes/1/core_tasks/10">Homework 1</a>
    <div class="f-tile__description">
      <span>Apr 15</span><a href="/student/classes/1">Math HL</a>
    </div>
  </div>
</body></html>"""


@pytest.fixture()
def client(tmp_path: Path):
    """A client wired to a temp cache with no retry delay."""
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    c = ManageBacClient("myschool", domain="managebac.cn", cache=cache, retry=0)
    c.request_delay = 0.0
    c.set_cookie("test_session_cookie")
    return c


@pytest.fixture()
def bare_client():
    """A client with the cache disabled — proves behaviour that is not a cache artefact."""
    cache = ResponseCache(enabled=False)
    c = ManageBacClient("myschool", domain="managebac.cn", cache=cache, retry=0)
    c.request_delay = 0.0
    c.set_cookie("test_session_cookie")
    return c


def _as_bytes(value) -> bytes:
    """requests_mock records form bodies as str and raw bodies as bytes."""
    if value is None:
        return b""
    return value if isinstance(value, bytes) else str(value).encode()


def _foreign_requests(mocker) -> list:
    return [r for r in mocker.request_history if "evil.example.com" in r.url]


# ── Defect 1: the cross-host guard must prevent the send ──────────────────


class TestCrossHostGuardPreventsTheSend:
    """No request may ever be *issued* to a host outside the ManageBac estate.

    ``requests`` follows redirects by merging the whole cookie jar into the new
    target and, on 307/308, replaying the request body.  Checking the final URL
    after the fact therefore detects the exfiltration instead of preventing it:
    the login POST body carries the plaintext password, so a redirect off the
    estate would already have shipped it.
    """

    def test_307_off_estate_never_receives_the_login_body(self, client):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/login",
                text='<html><input name="authenticity_token" value="tok123"></html>',
            )
            m.post(
                f"{BASE}/sessions",
                status_code=307,
                headers={"Location": "https://evil.example.com/collect"},
            )
            m.post("https://evil.example.com/collect", text="stolen")

            with pytest.raises(CommandError) as excinfo:
                client.login("user@example.com", "SUPERSECRET")

        assert excinfo.value.code == "cross_host_redirect_blocked"
        # The guard must be a pre-send gate, not a post-hoc check.
        assert _foreign_requests(m) == [], "a request was issued to a foreign host"
        # The password belongs in the POST to ManageBac; it must appear in no
        # request to any other origin, whatever that request was.
        leaked = [
            r.url
            for r in m.request_history
            if b"SUPERSECRET" in _as_bytes(r.body)
            and not r.url.startswith(f"{BASE}/")
        ]
        assert leaked == [], f"the password left the estate via {leaked}"

    def test_302_off_estate_never_receives_the_session_cookie(self, bare_client):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/student/dashboard",
                status_code=302,
                headers={"Location": "https://evil.example.com/collect"},
            )
            m.get("https://evil.example.com/collect", text="stolen")

            with pytest.raises(CommandError) as excinfo:
                bare_client._get("/student/dashboard", bypass_cache=True)

        assert excinfo.value.code == "cross_host_redirect_blocked"
        assert _foreign_requests(m) == []

    def test_downgrade_to_http_is_blocked(self, client):
        """A plaintext hop would ship the cookie in cleartext even on our own name."""
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/student/dashboard",
                status_code=302,
                headers={"Location": "http://myschool.managebac.cn/dashboard"},
            )
            m.get("http://myschool.managebac.cn/dashboard", text="downgraded")

            with pytest.raises(CommandError) as excinfo:
                client._get("/student/dashboard", bypass_cache=True)

        assert excinfo.value.code == "insecure_redirect_blocked"
        assert not any(r.url.startswith("http://") for r in m.request_history)

    def test_same_host_307_still_replays_the_body(self, client):
        """Blocking foreign redirects must not break legitimate same-host ones.

        ManageBac uses 307/308 for form resubmission on its own host; the body
        has to survive the hop or real flows break.
        """
        with rm.Mocker() as m:
            m.post(
                f"{BASE}/sessions",
                status_code=307,
                headers={"Location": f"{BASE}/sessions/reissue"},
            )
            m.post(f"{BASE}/sessions/reissue", text="<html>ok</html>")

            r = client._request_with_retry("POST", f"{BASE}/sessions", data={"a": "b"})

        assert r.status_code == 200
        replayed = [h for h in m.request_history if h.url.endswith("/reissue")]
        assert len(replayed) == 1
        assert b"a=b" in _as_bytes(replayed[0].body), "the 307 body was not replayed"

    def test_same_host_302_chain_is_followed(self, client):
        """login() depends on following ManageBac's own redirect to the dashboard."""
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/login",
                text='<html><input name="authenticity_token" value="tok"></html>',
            )
            m.post(
                f"{BASE}/sessions",
                status_code=302,
                headers={"Location": f"{BASE}/dashboard"},
            )
            m.get(
                f"{BASE}/dashboard",
                status_code=302,
                headers={"Location": f"{BASE}/student/tasks_and_deadlines"},
            )
            m.get(f"{BASE}/student/tasks_and_deadlines", text=TASKS_PAGE)

            assert client.login("user@example.com", "pw") is True
            assert m.request_history[-1].url == f"{BASE}/student/tasks_and_deadlines"


# ── Defect 2: a stale cache hit must not mask auth or security failures ───


class TestStaleCacheDoesNotMaskErrors:
    """A stale entry may paper over a *transient* failure, never a policy one.

    Serving stale grades after the session dies, or after a cross-host
    redirect was blocked, turns a hard stop into a silent wrong answer.
    """

    def _make_stale(self, client, url: str, body: str) -> None:
        client.cache.put(url, body, 200)
        client.cache.invalidate(url)  # ts=0 + invalidated → only reachable via allow_stale

    def test_expired_session_is_not_masked_by_stale_cache(self, client):
        url = f"{BASE}/student/classes/1/core_tasks"
        self._make_stale(client, url, "<html><body>LAST CYCLE GRADES</body></html>")

        with rm.Mocker() as m:
            m.get(url, text=LOGIN_PAGE)  # 200, but it is the login page

            with pytest.raises(RuntimeError, match="expired"):
                client.get_class_grades("1")

    def test_cross_host_block_is_not_masked_by_stale_cache(self, client):
        url = f"{BASE}/student/dashboard"
        self._make_stale(client, url, "<html><body>STALE CLASSES</body></html>")

        with rm.Mocker() as m:
            m.get(
                url,
                status_code=302,
                headers={"Location": "https://evil.example.com/collect"},
            )
            m.get("https://evil.example.com/collect", text="stolen")

            with pytest.raises(CommandError) as excinfo:
                client.get_classes()

        assert excinfo.value.code == "cross_host_redirect_blocked"
        assert _foreign_requests(m) == []

    def test_transient_failure_still_serves_stale(self, client, caplog):
        """The stale fallback is legitimate for transport blips — keep it."""
        url = f"{BASE}/student/dashboard"
        self._make_stale(client, url, "<html><body>STALE CLASSES</body></html>")

        with rm.Mocker() as m:
            m.get(url, exc=__import__("requests").ConnectionError("network down"))

            soup = client._get("/student/dashboard")

        assert "STALE CLASSES" in soup.get_text()
        assert "stale" in caplog.text.lower()

    def test_login_page_is_not_written_to_the_cache(self, client):
        url = f"{BASE}/student/tasks_and_deadlines?view=upcoming&page=1"
        with rm.Mocker() as m:
            m.get(url, text=LOGIN_PAGE)
            with pytest.raises(RuntimeError, match="expired"):
                client.get_tasks_by_view("upcoming", max_pages=1)

        assert client.cache.get(url) is None


# ── Defect 3: the scraped MNN hub endpoint must be validated ──────────────


class TestScrapedHubEndpointIsValidated:
    """The hub JWT may only go to a Faria-operated hub origin over TLS.

    ``data-mnn-hub-endpoint`` is scraped from ManageBac HTML, so a compromised
    page or a MITM chooses it.  Sending the Bearer token anywhere else — or over
    cleartext — hands over the notification session.
    """

    def test_hostile_host_is_rejected(self, client):
        assert (
            client._validated_hub_endpoint("https://collector.attacker.test/hub")
            == "https://mnn-hub.prod.faria.cn"
        )

    def test_cleartext_endpoint_is_rejected(self, client):
        assert (
            client._validated_hub_endpoint("http://mnn-hub.prod.faria.cn")
            == "https://mnn-hub.prod.faria.cn"
        )

    def test_websocket_scheme_is_rejected(self, client):
        assert (
            client._validated_hub_endpoint("wss://mnn-hub.prod.faria.cn/hub")
            == "https://mnn-hub.prod.faria.cn"
        )

    def test_userinfo_spoof_is_rejected(self, client):
        assert (
            client._validated_hub_endpoint("https://mnn-hub.prod.faria.cn@evil.test/hub")
            == "https://mnn-hub.prod.faria.cn"
        )

    def test_known_faria_host_is_accepted(self, client):
        assert (
            client._validated_hub_endpoint("https://mnn-hub.prod.faria.com")
            == "https://mnn-hub.prod.faria.com"
        )

    def test_empty_endpoint_falls_back_to_the_domain_default(self, client):
        assert client._validated_hub_endpoint("") == "https://mnn-hub.prod.faria.cn"

    def test_jwt_is_never_sent_to_a_hostile_scraped_host(self, client):
        hostile_page = (
            '<html><body><a class="js-messages-and-notifications-trigger" '
            'data-token="eyJhbGciOiJIUzI1NiJ9.test.token" '
            'data-mnn-hub-endpoint="https://collector.attacker.test/hub"></a>'
            "</body></html>"
        )
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/tasks_and_deadlines"), text=TASKS_PAGE
            )
            m.get(f"{BASE}/student/notifications", text=hostile_page)
            m.get(f"{BASE}/student/classes/1/core_tasks", text="<html></html>")
            result = client.crawl_index()

        assert not any(
            "collector.attacker.test" in r.url for r in m.request_history
        ), "the hub JWT was sent to an attacker-chosen host"
        assert not any(
            "eyJhbGciOiJIUzI1NiJ9" in str(r.headers.get("Authorization", ""))
            for r in m.request_history
            if "collector.attacker.test" in r.url
        )
        assert result["notifications"]["unread_count"] == 0


# ── Defect 4: submit_file must verify the upload ──────────────────────────


class TestSubmitFileVerifiesTheUpload:
    """``ok: True`` must mean the coursework actually landed.

    ManageBac answers rejected uploads with a 200 and a human-readable error,
    so a status-code check alone reports success for a failed submission.
    """

    def _dropbox_page(self):
        return (
            '<html><head><meta name="csrf-token" content="csrf"></head><body>'
            '<form id="edit_dropbox_123" action="/x/dropbox"></form></body></html>'
        )

    def _submit(self, client, tmp_path: Path, body: str, status: int = 200, json_body=None):
        f = tmp_path / "work.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/.+/core_tasks/.+/dropbox$"),
                text=self._dropbox_page(),
            )
            if json_body is not None:
                m.post(
                    f"{BASE}/student/classes/1000023/core_tasks/1000026/dropbox/upload",
                    json=json_body,
                    status_code=status,
                )
            else:
                m.post(
                    f"{BASE}/student/classes/1000023/core_tasks/1000026/dropbox/upload",
                    text=body,
                    status_code=status,
                )
            return client.submit_file("1000023", "1000026", str(f))

    def test_200_with_a_failure_message_is_not_reported_as_success(
        self, client, tmp_path: Path
    ):
        with pytest.raises(RuntimeError, match="permitted"):
            self._submit(
                client,
                tmp_path,
                "File type not permitted. Please try again.",
                status=200,
            )

    def test_json_error_envelope_is_not_reported_as_success(
        self, client, tmp_path: Path
    ):
        with pytest.raises(RuntimeError):
            self._submit(
                client, tmp_path, "", status=200, json_body={"ok": False, "error": "rejected"}
            )

    def test_http_error_is_still_reported_as_failure(self, client, tmp_path: Path):
        with pytest.raises(Exception):
            self._submit(client, tmp_path, "boom", status=422)

    def test_genuine_success_is_still_reported_as_success(self, client, tmp_path: Path):
        result = self._submit(client, tmp_path, "", status=200, json_body={"ok": True})
        assert result["ok"] is True
        assert result["filename"] == "work.pdf"


# ── Defect 5: chart data-series must handle [ts, value] pairs ─────────────


class TestExpectedGradeHandlesBothSeriesShapes:
    """A malformed point must skip, not take the whole class down.

    ManageBac emits Highcharts series in two shapes; the ``[[ts, value], ...]``
    form raised ``TypeError`` out of ``get_class_grades``, which the per-class
    ``except`` in ``crawl_all`` swallowed as a warning — the class vanished from
    the output and grade frequencies undercounted silently.
    """

    def _grades_page(self, data_series: str) -> str:
        return (
            "<html><body>"
            '<div class="assignments-progress-chart"'
            ' data-grade-labels=\'{"0":"F","1":"E","2":"D","3":"C","4":"B","5":"A"}\''
            f' data-series=\'{data_series}\'>'
            "</div>"
            '<div class="fusion-card-item"><h4 class="title">'
            '<a href="/student/classes/1/core_tasks/9">T</a></h4>'
            '<span class="grade">A</span></div>'
            "</body></html>"
        )

    def test_timestamp_value_pairs(self, client):
        series = '[{"name":"Term","data":[[1700000000000,4]]}]'
        with rm.Mocker() as m:
            m.get(f"{BASE}/student/classes/1/core_tasks", text=self._grades_page(series))
            result = client.get_class_grades("1")

        expected = result["expected_grade"]
        assert expected is not None, "timestamp/value series produced no expected grade"
        assert expected["num_graded"] == 1
        assert expected["average_score"] == 4.0
        assert expected["letter_grade"] == "B"

    def test_flat_values_still_work(self, client):
        series = '[{"name":"Homework 1","data":[4]},{"name":"Essay","data":[5]}]'
        with rm.Mocker() as m:
            m.get(f"{BASE}/student/classes/1/core_tasks", text=self._grades_page(series))
            result = client.get_class_grades("1")

        expected = result["expected_grade"]
        assert expected is not None
        assert expected["num_graded"] == 2
        assert expected["average_score"] == 4.5

    def test_unparseable_points_are_skipped_not_raised(self, client):
        series = '[{"name":"Junk","data":[["x","y"]]},{"name":"Good","data":[[1,5]]}]'
        with rm.Mocker() as m:
            m.get(f"{BASE}/student/classes/1/core_tasks", text=self._grades_page(series))
            result = client.get_class_grades("1")

        assert result["expected_grade"] is not None
        assert result["expected_grade"]["num_graded"] == 1
        assert len(result["tasks"]) == 1, "the class lost its tasks to a chart parse error"

    def test_class_survives_a_malformed_series_in_crawl_all(self, client):
        series = '[{"name":"Term","data":[[1700000000000,4]]}]'
        with rm.Mocker() as m:
            m.get(f"{BASE}/student/dashboard", text='<a href="/student/classes/1">Math</a>')
            m.get(f"{BASE}/student/classes/1/core_tasks", text=self._grades_page(series))
            m.get(f"{BASE}/student/notifications", text="<html>none</html>")
            result = client.crawl_all(max_pages=1)

        assert result["summary"]["upcoming_count"] + result["summary"]["past_count"] + result[
            "summary"
        ]["overdue_count"] == 1


# ── Defect 6: pagination detection ────────────────────────────────────────


class TestPaginationDetection:
    """``page=2`` is not ``page=20``, and a "next lesson" is not a next page.

    Both false positives fetched out-of-range pages.  A 404 there aborted the
    whole crawl, and a server echoing page 1 duplicated every task.
    """

    def _soup(self, html: str):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser")

    def test_page_20_does_not_satisfy_a_search_for_page_2(self, client):
        html = '<html><body><a href="?view=upcoming&page=20">20</a>'
        html += '<a href="?view=upcoming&page=24">24</a></body></html>'
        assert client._has_next_page(self._soup(html), 1, "upcoming") is False

    def test_exact_page_number_is_still_detected(self, client):
        html = '<html><body><a href="?view=upcoming&page=2">2</a></body></html>'
        assert client._has_next_page(self._soup(html), 1, "upcoming") is True

    def test_next_lesson_link_is_not_pagination(self, client):
        html = '<html><body><a aria-label="Next lesson" href="/x">Next lesson</a></body></html>'
        assert client._has_next_page(self._soup(html), 1, "upcoming") is False

    def test_next_page_control_is_still_detected(self, client):
        html = '<html><body><button class="next" aria-label="Next page">></button></body></html>'
        assert client._has_next_page(self._soup(html), 1, "upcoming") is True

    def test_rel_next_is_still_detected(self, client):
        html = '<html><body><a rel="next" href="?page=2">Next</a></body></html>'
        assert client._has_next_page(self._soup(html), 1, "upcoming") is True

    def test_out_of_range_page_does_not_duplicate_tasks(self, client):
        """A server that echoes page 1 for out-of-range pages must not double tasks."""
        page1 = (
            '<html><body><a href="/student/profile">Test Student — ManageBac</a>'
            '<a href="?view=upcoming&page=20">20</a>'
            '<div class="f-task-tile"><a class="f-tile__title-link" '
            'href="/student/classes/1/core_tasks/10">Homework 1</a></div></body></html>'
        )
        with rm.Mocker() as m:
            m.get(re.compile(r"page=1"), text=page1)
            m.get(re.compile(r"page=2"), text=page1)  # echo of page 1
            tasks = client.get_tasks_by_view("upcoming", max_pages=5)

        assert [t["id"] for t in tasks] == ["10"], tasks

    def test_duplicate_tiles_on_distinct_pages_are_deduped(self, client):
        page1 = (
            '<html><body><a href="?view=upcoming&page=2">2</a>'
            '<div class="f-task-tile"><a class="f-tile__title-link" '
            'href="/student/classes/1/core_tasks/10">Homework 1</a></div></body></html>'
        )
        page2 = (
            '<html><body><div class="f-task-tile">'
            '<a class="f-tile__title-link" href="/student/classes/1/core_tasks/10">'
            "Homework 1</a></div>"
            '<div class="f-task-tile"><a class="f-tile__title-link" '
            'href="/student/classes/1/core_tasks/11">Homework 2</a></div></body></html>'
        )
        with rm.Mocker() as m:
            m.get(re.compile(r"page=1"), text=page1)
            m.get(re.compile(r"page=2"), text=page2)
            tasks = client.get_tasks_by_view("upcoming", max_pages=5)

        assert [t["id"] for t in tasks] == ["10", "11"]


# ── Defect 7: the login page must be detected by its body ─────────────────


class TestLoginPageDetectedByBody:
    """A 200 rendering the login form means the session is dead.

    The old check looked for ``/login`` in the *requested* URL, which no real
    path contains, so only an actual redirect was caught.  A login page served
    at 200 was parsed as an empty task list and cached for the whole TTL.
    """

    def test_login_page_at_200_raises(self, client):
        url = f"{BASE}/student/tasks_and_deadlines?view=upcoming&page=1"
        with rm.Mocker() as m:
            m.get(url, text=LOGIN_PAGE)
            with pytest.raises(RuntimeError, match="expired"):
                client.get_tasks_by_view("upcoming", max_pages=1)

    def test_login_page_at_200_is_not_cached(self, client):
        url = f"{BASE}/student/tasks_and_deadlines?view=upcoming&page=1"
        with rm.Mocker() as m:
            m.get(url, text=LOGIN_PAGE)
            with pytest.raises(RuntimeError):
                client._get("/student/tasks_and_deadlines?view=upcoming&page=1")
        assert client.cache.get(url) is None

    def test_redirect_to_login_still_raises(self, client):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/student/tasks_and_deadlines?view=upcoming&page=1",
                status_code=302,
                headers={"Location": "/login"},
            )
            m.get(f"{BASE}/login", text=LOGIN_PAGE)
            with pytest.raises(RuntimeError, match="expired"):
                client.get_tasks_by_view("upcoming", max_pages=1)

    def test_a_page_with_a_password_field_but_no_login_form_is_not_treated_as_login(
        self, client
    ):
        """A change-password page must not be mistaken for an expired session."""
        page = (
            "<html><body><h1>Change password</h1>"
            "<form action='/student/profile/password'>"
            "<input type='password' name='current_password'></form></body></html>"
        )
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/student/tasks_and_deadlines?view=upcoming&page=1", text=page
            )
            assert client.get_tasks_by_view("upcoming", max_pages=1) == []


# ── Defect 8: no request path may bypass the host guard ───────────────────


class TestNoPathBypassesTheHostGuard:
    """Every request that carries the session cookie goes through one gate."""

    def _submission_page(self) -> str:
        return (
            '<html><head><meta name="csrf-token" content="csrf123"></head><body>'
            '<form id="edit_dropbox_17874401"></form><table>'
            '<tr class="file" id="asset_82189817"><td>'
            '<a class="text-break" href="/uploads/asset/file/82189817/f.pdf">f.pdf</a>'
            '<label>Uploaded Sep 13, 2026</label></td><td>'
            '<a class="btn-remove" data-method="delete" '
            'href="/student/dropboxes/17874401/destroy_asset?file_id=82189817">Delete</a>'
            "</td></tr></table></body></html>"
        )

    def test_delete_submission_refuses_a_redirect_off_estate(self, client):
        with rm.Mocker() as m:
            m.get(
                re.compile(r"/student/classes/1/core_tasks/9"),
                text=self._submission_page(),
            )
            m.delete(
                f"{BASE}/student/dropboxes/17874401/destroy_asset",
                status_code=302,
                headers={"Location": "https://evil.example.com/collect"},
            )
            m.get("https://evil.example.com/collect", text="stolen")

            with pytest.raises(CommandError) as excinfo:
                client.delete_submission("1", "9", "82189817")

        assert excinfo.value.code == "cross_host_redirect_blocked"
        assert _foreign_requests(m) == []

    def test_delete_submission_is_retried_on_transient_failure(self, client, monkeypatch):
        """Routing through the wrapper must buy the DELETE the retry it lost.

        Also proves the post-delete verification still runs: the second read of
        the task page shows the row gone, which is what makes ``ok`` honest.
        """
        client.retry = 1
        monkeypatch.setattr("mb_cli.client.time.sleep", lambda _s: None)

        deletes = {"n": 0}
        reads = {"n": 0}
        empty_page = (
            "<html><head><meta name='csrf-token' content='csrf123'></head>"
            "<body><form id='edit_dropbox_17874401'></form>"
            "<table></table></body></html>"
        )

        def _read_task_page(request, context):
            # Reads 1-2 are delete_submission's own lookups (CSRF, then
            # get_submissions); read 3 is the post-delete verification, which
            # must see the row gone or delete_submission rightly reports failure.
            reads["n"] += 1
            context.status_code = 200
            return self._submission_page() if reads["n"] <= 2 else empty_page

        def _delete(request, context):
            deletes["n"] += 1
            if deletes["n"] == 1:
                context.status_code = 503
                return "busy"
            context.status_code = 200
            return ""

        with rm.Mocker() as m:
            m.get(re.compile(r"/student/classes/1/core_tasks/9$"), text=_read_task_page)
            m.delete(re.compile(r"/destroy_asset"), json=_delete)

            result = client.delete_submission("1", "9", "82189817")

        assert deletes["n"] == 2, "a retryable 503 was not retried"
        assert result["ok"] is True
        assert result["filename"] == "f.pdf"

    def test_feedback_modal_get_refuses_a_redirect_off_estate(self, client, caplog):
        with rm.Mocker() as m:
            m.get(
                f"{BASE}/student/classes/1/core_tasks/9/feedback",
                text="<html></html>",
            )
            m.get(
                f"{BASE}/preview/1",
                status_code=302,
                headers={"Location": "https://evil.example.com/collect"},
            )
            m.get("https://evil.example.com/collect", text="stolen")

            result = client._parse_feedback_page(
                f"{BASE}/student/classes/1/core_tasks/9/feedback",
                preview_modal_url="/preview/1",
            )

        assert _foreign_requests(m) == []
        # The guard fired before the send; _parse_feedback_page swallows modal
        # errors into a log line, so that is where the evidence lands.
        assert "Refusing to send authenticated request to 'evil.example.com'" in (
            caplog.text
        )
        assert result["annotated_download_url"] is None

    def test_foreign_attachment_url_is_dropped(self, client):
        from bs4 import BeautifulSoup

        html = (
            '<div><a href="https://cdn.evil.test/payload.pdf" class="fr-file">'
            "payload.pdf</a></div>"
        )
        assert client._extract_attachments(BeautifulSoup(html, "html.parser")) == []

    def test_cleartext_attachment_url_is_dropped(self, client):
        from bs4 import BeautifulSoup

        html = (
            '<div><a href="http://myschool.managebac.cn/uploads/x/plain.pdf" class="fr-file">'
            "plain.pdf</a></div>"
        )
        assert client._extract_attachments(BeautifulSoup(html, "html.parser")) == []

    def test_same_estate_attachment_is_kept(self, client):
        from bs4 import BeautifulSoup

        html = (
            '<div><a href="/student/classes/1/attachments/123/report.pdf" '
            'class="fr-file">report.pdf</a></div>'
        )
        atts = client._extract_attachments(BeautifulSoup(html, "html.parser"))
        assert len(atts) == 1
        assert atts[0]["url"] == f"{BASE}/student/classes/1/attachments/123/report.pdf"


# ── Defect 9: the attachment dedup key must keep the query string ─────────


class TestAttachmentDedupKeepsQuery:
    """``?v=1`` and ``?v=2`` are different files; dropping the query loses one."""

    def test_query_string_distinguishes_attachments(self, client):
        from bs4 import BeautifulSoup

        html = (
            "<div>"
            '<a href="/student/classes/1/attachments/1/essay.pdf?v=1" class="fr-file">essay.pdf</a>'
            '<a href="/student/classes/1/attachments/1/essay.pdf?v=2" class="fr-file">essay.pdf</a>'
            "</div>"
        )
        atts = client._extract_attachments(BeautifulSoup(html, "html.parser"))
        assert len(atts) == 2, atts

    def test_truly_identical_links_still_dedupe(self, client):
        from bs4 import BeautifulSoup

        html = (
            "<div>"
            '<a href="/student/classes/1/attachments/1/f.pdf" class="fr-file">f.pdf</a>'
            '<a href="/student/classes/1/attachments/1/f.pdf" class="fr-file">f.pdf</a>'
            "</div>"
        )
        atts = client._extract_attachments(BeautifulSoup(html, "html.parser"))
        assert len(atts) == 1


# ── Defect 10: parse_due_date must return one unambiguous type ────────────


