"""Find what transport ManageBac's OWN frontend uses for live notifications.

The MNN hub exposes polled REST only, but the notification bell in the browser
has to update somehow. If the site's JavaScript opens a socket or an
EventSource, that transport is the one to build on — and it would be a real
push channel, not polling.

Read-only. Session cookie only, no password. Writes nothing.
"""

from __future__ import annotations

import os
import re
import sys

from mb_cli.auth import build_client

assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

# Things that would indicate a live channel, and what each one means.
PATTERNS = {
    "ActionCable (Rails WS)": r"ActionCable|createConsumer|App\.cable",
    "WebSocket ctor": r"new\s+WebSocket\s*\(",
    "ws:// or wss:// URL": r"wss?://[^\s\"'<>]+",
    "EventSource (SSE)": r"new\s+EventSource\s*\(|EventSource\s*=",
    "socket.io": r"socket\.io|io\.connect",
    "Pusher": r"pusher|Pusher\(",
    "Firebase/FCM": r"firebase|fcm|messaging\(\)",
    "long-poll / setInterval poll": r"setInterval\s*\(|pollNotifications|startPolling",
    "Service Worker push": r"serviceWorker\.register|pushManager\.subscribe",
    "mnn hub references": r"mnn[-_]?hub|mnnHub",
}

PAGES = (
    "/student/notifications",
    "/student/dashboard",
)


def main() -> int:
    state, client, email = build_client()
    print(f"authenticated as {email}; scanning {len(PAGES)} pages for live-channel code\n")

    hits: dict[str, list[str]] = {}
    script_urls: list[str] = []

    for path in PAGES:
        try:
            r = client.session.get(f"{client.base}{path}", timeout=20)
        except Exception as exc:
            print(f"{path}: {type(exc).__name__}: {exc}")
            continue
        html = r.text
        print(f"{path}: HTTP {r.status_code}, {len(html)} bytes")

        for label, pat in PATTERNS.items():
            for m in re.finditer(pat, html, re.IGNORECASE):
                snippet = html[max(0, m.start() - 60):m.end() + 60]
                snippet = " ".join(snippet.split())
                hits.setdefault(label, []).append(f"{path}: …{snippet}…")

        for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html):
            script_urls.append((path, src))

    print(f"\n── inline findings in page HTML")
    if not hits:
        print("  none")
    for label, found in hits.items():
        print(f"  {label}: {len(found)} hit(s)")
        for f in found[:3]:
            print(f"    {f[:200]}")

    print(f"\n── script tags ({len(script_urls)})")
    for page, src in script_urls[:40]:
        print(f"  {src[:120]}")

    # The interesting code is usually in a bundled JS asset, not the HTML.
    print(f"\n── scanning fetched scripts for live-channel code")
    seen: set[str] = set()
    script_hits: dict[str, list[str]] = {}
    for page, src in script_urls:
        url = src if src.startswith("http") else client.base + (
            src if src.startswith("/") else "/" + src
        )
        if url in seen or "jquery" in url.lower():
            continue
        seen.add(url)
        try:
            r = client.session.get(url, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        js = r.text
        print(f"  {url[-70:]} -> {r.status_code}, {len(js)} bytes")
        for label, pat in PATTERNS.items():
            for m in re.finditer(pat, js, re.IGNORECASE):
                snippet = js[max(0, m.start() - 80):m.end() + 80]
                snippet = " ".join(snippet.split())
                script_hits.setdefault(label, []).append(f"{url[-50:]}: …{snippet}…")

    print(f"\n── live-channel code found in scripts")
    if not script_hits:
        print("  none — no WebSocket, ActionCable, socket.io, Pusher, FCM,")
        print("  EventSource, or service-worker push anywhere in the scanned assets.")
    for label, found in sorted(script_hits.items()):
        print(f"  {label}: {len(found)} hit(s)")
        for f in found[:4]:
            print(f"    {f[:220]}")

    ws_urls = script_hits.get("ws:// or wss:// URL", [])
    if ws_urls:
        print(f"\n  *** CANDIDATE PUSH URLS ***")
        for u in ws_urls:
            print(f"    {u[:250]}")

    print("\n── verdict")
    if script_hits.get("ws:// or wss:// URL") or script_hits.get(
        "EventSource (SSE)"
    ) or script_hits.get("ActionCable (Rails WS)"):
        print("  The frontend uses a live channel. Build on it — true push is possible.")
    elif script_hits.get("long-poll / setInterval poll"):
        print("  The frontend itself polls. There is no push channel to inherit:")
        print("  'real-time' for tahuti means fast conditional-request polling,")
        print("  which the hub does support (Etag/304 on both endpoints).")
    else:
        print("  Inconclusive from static scanning — check the browser's Network")
        print("  tab, WS filter, with the notifications page open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
