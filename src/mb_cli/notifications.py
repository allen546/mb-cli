"""MNN Hub notification client for ManageBac."""

from __future__ import annotations

import random
import time

import requests

HUB_ENDPOINTS = {
    "managebac.com": "https://mnn-hub.prod.faria.com",
    "managebac.cn": "https://mnn-hub.prod.faria.cn",
}


def hub_for_domain(domain: str) -> str:
    return HUB_ENDPOINTS.get(domain, HUB_ENDPOINTS["managebac.com"])


class MNNHubClient:
    """REST client for the ManageBac Notification Network hub."""

    def __init__(self, endpoint: str, token: str, verify: bool | str = True):
        self.base = f"{endpoint}/api/frontend/v2"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Content-Type"] = "application/json"
        self.session.verify = verify

    def _jitter(self) -> None:
        time.sleep(random.uniform(0.3, 1.5))

    # ── Read ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        self._jitter()
        r = self.session.get(f"{self.base}/notifications/stats")
        r.raise_for_status()
        return r.json().get("stats", {})

    def list(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        filter_: str = "all",
    ) -> dict:
        params: dict = {"page": page, "per_page": per_page}
        if filter_ and filter_ != "all":
            params["filter"] = filter_
        self._jitter()
        r = self.session.get(f"{self.base}/notifications", params=params)
        r.raise_for_status()
        data = r.json()
        return {
            "items": data.get("items", []),
            "meta": data.get("meta", {}),
        }

    # ── Mutate ──────────────────────────────────────────────────────────

    def mark_read(self, notification_id: int) -> bool:
        self._jitter()
        r = self.session.put(f"{self.base}/notifications/{notification_id}/read")
        return r.status_code == 204

    def mark_unread(self, notification_id: int) -> bool:
        self._jitter()
        r = self.session.put(f"{self.base}/notifications/{notification_id}/unread")
        return r.status_code == 204

    def mark_all_read(self) -> bool:
        self._jitter()
        r = self.session.put(f"{self.base}/notifications/mark_as_read")
        return r.status_code == 204

    def star(self, notification_id: int) -> bool:
        self._jitter()
        r = self.session.put(f"{self.base}/notifications/{notification_id}/star")
        return r.status_code == 204

    def unstar(self, notification_id: int) -> bool:
        self._jitter()
        r = self.session.put(f"{self.base}/notifications/{notification_id}/unstar")
        return r.status_code == 204
