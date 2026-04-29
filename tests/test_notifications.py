"""Tests for mb_cli.notifications."""

from __future__ import annotations

import pytest
import requests_mock

from mb_cli.notifications import HUB_ENDPOINTS, MNNHubClient, hub_for_domain


class TestHubForDomain:
    def test_managebac_com(self):
        assert hub_for_domain("managebac.com") == "https://mnn-hub.prod.faria.com"

    def test_managebac_cn(self):
        assert hub_for_domain("managebac.cn") == "https://mnn-hub.prod.faria.cn"

    def test_unknown_defaults_to_com(self):
        assert hub_for_domain("unknown.domain") == "https://mnn-hub.prod.faria.com"


class TestMNNHubClient:
    @pytest.fixture()
    def hub(self):
        return MNNHubClient("https://mnn-hub.prod.faria.com", "test_token")

    def test_base_url(self, hub):
        assert hub.base == "https://mnn-hub.prod.faria.com/api/frontend/v2"

    def test_auth_header(self, hub):
        assert hub.session.headers["Authorization"] == "Bearer test_token"

    def test_content_type(self, hub):
        assert hub.session.headers["Content-Type"] == "application/json"

    def test_stats(self, hub):
        with requests_mock.Mocker() as m:
            m.get(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/stats",
                json={"stats": {"unread_messages": 5}},
            )
            result = hub.stats()
            assert result["unread_messages"] == 5

    def test_list(self, hub):
        with requests_mock.Mocker() as m:
            m.get(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications",
                json={
                    "items": [{"id": 1, "title": "Test"}],
                    "meta": {"page": 1, "total": 10},
                },
            )
            result = hub.list(page=1, per_page=20)
            assert len(result["items"]) == 1
            assert result["meta"]["total"] == 10

    def test_list_with_filter(self, hub):
        with requests_mock.Mocker() as m:
            m.get(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications",
                json={"items": [], "meta": {}},
            )
            hub.list(filter_="unread")
            assert "filter=unread" in m.last_request.url

    def test_list_all_filter_not_sent(self, hub):
        with requests_mock.Mocker() as m:
            m.get(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications",
                json={"items": [], "meta": {}},
            )
            hub.list(filter_="all")
            assert "filter" not in m.last_request.url

    def test_mark_read(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/1234/read",
                status_code=204,
            )
            assert hub.mark_read(1234) is True

    def test_mark_read_failure(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/1234/read",
                status_code=404,
            )
            assert hub.mark_read(1234) is False

    def test_mark_unread(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/1234/unread",
                status_code=204,
            )
            assert hub.mark_unread(1234) is True

    def test_mark_all_read(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/mark_as_read",
                status_code=204,
            )
            assert hub.mark_all_read() is True

    def test_star(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/1234/star",
                status_code=204,
            )
            assert hub.star(1234) is True

    def test_unstar(self, hub):
        with requests_mock.Mocker() as m:
            m.put(
                "https://mnn-hub.prod.faria.com/api/frontend/v2/notifications/1234/unstar",
                status_code=204,
            )
            assert hub.unstar(1234) is True

    def test_verify_false(self):
        hub = MNNHubClient("https://example.com", "tok", verify=False)
        assert hub.session.verify is False
