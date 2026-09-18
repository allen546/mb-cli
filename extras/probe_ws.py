"""Raw WebSocket handshake probe — requests cannot do ws://, so this speaks the
upgrade by hand over TLS. A hub that supports WS answers 101 Switching Protocols.

Read-only. Session cookie only, no password. Writes nothing.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import sys

from mb_cli.auth import build_client
from mb_cli.notifications import hub_for_domain

assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

# Fallback only. main() prefers the hub host resolved from the live session, so
# this works for .cn and .com alike and does not hardcode one into the sdist
# (/extras ships — see pyproject.toml).
HOST = "mnn-hub.prod.faria.cn"
FALLBACK_HOST = HOST
PATHS = (
    "/cable", "/websocket", "/ws", "/socket", "/api/frontend/v2/cable",
    "/api/frontend/v2/websocket", "/api/cable", "/realtime", "/api/frontend/v2/notifications/stream",
)


def probe(path: str, token: str) -> str:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Origin: https://myschool.managebac.cn\r\n"
        "\r\n"
    )
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((HOST, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                tls.sendall(req.encode())
                tls.settimeout(10)
                data = b""
                try:
                    while b"\r\n\r\n" not in data and len(data) < 8192:
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except socket.timeout:
                    return "no response in 10s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"

    if not data:
        return "connection closed with no response"
    status = data.split(b"\r\n", 1)[0].decode("latin-1")
    return status


def main() -> int:
    global HOST
    state, client, email = build_client()
    endpoint, token = client.get_notification_token(bypass_cache=True)
    if not endpoint:
        endpoint = hub_for_domain(client.domain)
    # Take the host from the resolved endpoint so .com and .cn are both handled.
    HOST = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    if not HOST:
        HOST = FALLBACK_HOST
    print(f"probing {HOST} for WebSocket upgrade (JWT present: {bool(token)})\n")

    saw_101 = False
    for path in PATHS:
        result = probe(path, token)
        mark = ""
        if "101" in result:
            saw_101 = True
            mark = "   <== WEBSOCKET CONFIRMED"
        elif "404" in result or "400" in result:
            mark = ""
        print(f"  {path:52s} {result}{mark}")

    print()
    if saw_101:
        print("VERDICT: the hub speaks WebSocket. A true push transport is possible.")
        return 0
    print("VERDICT: no path returned 101 — no WebSocket on the MNN hub.")
    print("Combined with the 404s on every SSE path, the hub is polled REST only.")
    print("'Real-time' must therefore mean: fast polling of the cheap stats")
    print("endpoint (32 bytes, unread count) plus a full fetch when it moves.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
