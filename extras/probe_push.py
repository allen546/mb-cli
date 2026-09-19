"""Probe the MNN hub for a real-time push transport.

Read-only. Answers one question: does the hub offer WebSocket / SSE /
long-polling, or is polled REST the only option? Decides whether the daemon's
notification path can ever become genuinely push-based.

Uses the saved session cookie only — never the password, never creds.json.
Writes nothing. Safe to run against the live account.

Usage:
    cd /mnt/pi-data/tahuti
    MB_CRAWLER_CREDS_PATH=/nonexistent .venv/bin/python extras/probe_push.py
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

from tahuti.auth import build_client
from tahuti.notifications import hub_for_domain

# Never let this script see or touch real credentials.
assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def head(title: str) -> None:
    print(f"\n{BOLD}── {title} {RESET}")


def main() -> int:
    print(f"{BOLD}MNN hub push-transport probe{RESET}")
    print(f"{DIM}read-only; session cookie only; no password, no writes{RESET}")

    state, client, email = build_client()
    print(f"authenticated as {email} via {client.base}")

    # ── 1. Where does the hub actually live, and what does it hand out? ──
    head("1. Hub endpoint + token")
    try:
        endpoint, token = client.get_notification_token(bypass_cache=True)
    except Exception as exc:
        print(f"could not scrape the trigger: {exc}")
        endpoint, token = "", ""
    if not endpoint:
        endpoint = hub_for_domain(client.domain)
        print(f"no endpoint on the page — using the default {endpoint}")
    print(f"endpoint : {endpoint}")
    print(f"token    : {'present, %d chars' % len(token) if token else 'ABSENT'}")

    # Decode the JWT payload (no verification — we only want its claims) to see
    # whether it is scoped to a channel, which is what a push transport would
    # need.
    if token and token.count(".") == 2:
        import base64

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            print(f"JWT claims: {json.dumps(claims, indent=2)[:800]}")
        except Exception as exc:
            print(f"JWT payload undecodable: {exc}")
    elif token:
        print(f"token is not a JWT (dots={token.count('.')}) — opaque string")

    # ── 2. Does the hub advertise any streaming capability? ──
    head("2. Streaming transport probes")
    base_api = f"{endpoint}/api/frontend/v2"
    auth = {"Authorization": f"Bearer {token}"}

    # WebSocket upgrade: a hub that speaks WS answers 101 on the handshake.
    print("WebSocket handshake ... ", end="", flush=True)
    ws_url = endpoint.replace("https://", "wss://").replace("http://", "ws://")
    for path in ("/cable", "/websocket", "/ws", "/socket", "/api/frontend/v2/cable"):
        try:
            r = requests.get(
                ws_url + path,
                headers={**auth, "Upgrade": "websocket", "Connection": "Upgrade",
                         "Sec-WebSocket-Version": "13",
                         "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
                timeout=8,
                allow_redirects=False,
            )
            print(f"{path} -> {r.status_code}", end="  ")
            if r.status_code == 101:
                print(f"\n  !! UPGRADE SUCCEEDED at {path} — the hub speaks WebSocket")
        except Exception as exc:
            print(f"{path} -> {type(exc).__name__}", end="  ")
    print()

    # SSE: an EventStream responds with Content-Type text/event-stream.
    print("SSE endpoints ... ", flush=True)
    for path in ("/events", "/stream", "/notifications/stream", "/notifications/live"):
        try:
            r = requests.get(
                f"{base_api}{path}", headers=auth, timeout=8,
                stream=True, allow_redirects=False,
            )
            ctype = r.headers.get("Content-Type", "")
            flag = "  <-- EVENT STREAM" if "text/event-stream" in ctype else ""
            print(f"  {path} -> {r.status_code} {ctype[:40]}{flag}")
            r.close()
        except Exception as exc:
            print(f"  {path} -> {type(exc).__name__}")

    # Long-poll: a long-poll blocks rather than returning immediately. Compare
    # wall time against a known-instant endpoint.
    print("Long-poll candidates ... ", flush=True)
    for path in ("/notifications?wait=true", "/notifications/poll",
                 "/notifications/since?timestamp=0", "/notifications/stats?long_poll=true"):
        try:
            import time

            t0 = time.monotonic()
            r = requests.get(f"{base_api}{path}", headers=auth, timeout=20)
            dt = time.monotonic() - t0
            print(f"  {path} -> {r.status_code} in {dt:.2f}s "
                  f"({'BLOCKED - long-poll?' if dt > 3 else 'instant'})")
        except requests.Timeout:
            print(f"  {path} -> TIMEOUT (blocked > 20s — likely a real long-poll)")
        except Exception as exc:
            print(f"  {path} -> {type(exc).__name__}")

    # ── 3. What does the known-good polled endpoint look like? ──
    head("3. Baseline polled REST (known working)")
    for meth, path, kw in (
        ("GET", "/notifications/stats", {}),
        ("GET", "/notifications?page=1&per_page=3", {}),
    ):
        try:
            r = requests.request(meth, f"{base_api}{path}", headers=auth,
                                 timeout=12, **kw)
            body = r.text[:300]
            print(f"  {meth} {path} -> {r.status_code}")
            print(f"    headers: {dict(list(r.headers.items())[:8])}")
            print(f"    body: {body}")
        except Exception as exc:
            print(f"  {meth} {path} -> {type(exc).__name__}: {exc}")

    # ── 4. Rate-limit / caching headers tell us the polling floor ──
    head("4. Rate-limit posture (sets the minimum poll interval)")
    try:
        r = requests.get(f"{base_api}/notifications/stats", headers=auth, timeout=12)
        for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                  "Retry-After", "Cache-Control", "Age", "X-Request-Id"):
            if h in r.headers:
                print(f"  {h}: {r.headers[h]}")
        if not any(h in r.headers for h in ("X-RateLimit-Limit", "Retry-After")):
            print("  no rate-limit headers — the API does not tell us its ceiling,")
            print("  so the current 1-3s jitter in notifications.py stays as-is")
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}")

    head("Interpretation")
    print(
        "If no path returned 101 and none returned text/event-stream, the hub is\n"
        "polled REST only. A 'real-time' feature then has to be one of:\n"
        "  (a) short-interval polling of /notifications/stats (cheap, unread count\n"
        "      only) with a full fetch only when the count moves;\n"
        "  (b) polling the ManageBac page itself — slower and heavier;\n"
        "  (c) an out-of-band push (Bark/Pushover/ntfy) that something else feeds.\n"
        "Note the current _jitter() sleeps 1-3s on EVERY call, which caps useful\n"
        "poll frequency at one request per 1-3s per endpoint."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
