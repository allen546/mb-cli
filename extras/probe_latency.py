"""Measure the real polling floor for the MNN hub stats endpoint.

The daemon's notification path currently calls MNNHubClient, whose _jitter()
sleeps a uniform 1-3 seconds on every single request. This measures what the
endpoint can actually do without that sleep, so we know the true latency floor
for a 'real-time' feature.

Read-only. Session cookie only, no password. Writes nothing.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import requests

from tahuti.auth import build_client
from tahuti.notifications import hub_for_domain

assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

N = 20


def main() -> int:
    state, client, email = build_client()
    endpoint, token = client.get_notification_token(bypass_cache=True)
    if not endpoint:
        endpoint = hub_for_domain(client.domain)
    url = f"{endpoint}/api/frontend/v2/notifications/stats"
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"Measuring {N} un-jittered requests to /notifications/stats\n")

    latencies: list[float] = []
    counts: list[int] = []
    etags: set[str] = set()
    for i in range(N):
        t0 = time.monotonic()
        try:
            r = requests.get(url, headers=auth, timeout=12)
        except Exception as exc:
            print(f"  request {i}: {type(exc).__name__}: {exc}")
            continue
        dt = time.monotonic() - t0
        latencies.append(dt)
        if r.status_code == 200:
            counts.append(r.json().get("stats", {}).get("unread_messages", -1))
            etags.add(r.headers.get("Etag", ""))
        else:
            print(f"  request {i}: HTTP {r.status_code}")
        print(f"  {i:2d}: {dt*1000:6.1f} ms  {r.status_code}")

    if not latencies:
        print("\nno successful requests")
        return 1

    print(f"\n{BOLD}round-trip latency{RESET}" if False else "\n── round-trip latency")
    print(f"  n      : {len(latencies)}")
    print(f"  min    : {min(latencies)*1000:.1f} ms")
    print(f"  median : {statistics.median(latencies)*1000:.1f} ms")
    print(f"  mean   : {statistics.mean(latencies)*1000:.1f} ms")
    print(f"  max    : {max(latencies)*1000:.1f} ms")
    print(f"\n── conditional-request support")
    print(f"  distinct Etags seen: {len(etags)} of {N}")
    print(f"  unread count stable : {len(set(counts)) <= 1} "
          f"(values seen: {sorted(set(counts))})")

    poll_floor = statistics.median(latencies)
    print(f"\n── implications for a real-time feature")
    print(f"  un-jittered median RTT : {poll_floor*1000:.0f} ms")
    print(f"  current effective floor: 1000-3000 ms (MNNHubClient._jitter)")
    print(f"  the jitter is the single biggest latency cost in the design")
    print(f"  at a 5s interval, {N} requests took {sum(latencies):.1f}s of network time")
    print(f"  vs {N * 2.0:.1f}s if _jitter() ran — i.e. jitter costs "
          f"{N*2.0/sum(latencies):.1f}x the transport itself")
    return 0


BOLD = "\033[1m"
RESET = "\033[0m"

if __name__ == "__main__":
    sys.exit(main())
