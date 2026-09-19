"""Try to actually USE the ManageBac WebSocket as a notification channel.

Established by earlier probes:
  - GET /websocket on <school>.managebac.cn answers 101 Switching Protocols
  - X-AnyCable-Version: 1.0.5-2b1cbd6  => AnyCable, speaking ActionCable protocol
  - the server sends {"type":"welcome","sid":...} then a {"type":"ping"} every 3s
  - the MNN hub itself (/cable etc.) 404s and has no cable code at all

This probe therefore tries the ActionCable subscribe handshake on the ManageBac
host across plausible channel names, and reports whether any of them carry
notification payloads. That is the difference between "a socket exists" and "we
can build real-time notifications on it".

Read-only apart from the subscribe frames, which the browser itself sends.
Session cookie only, no password. Writes nothing to disk.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time

from tahuti.auth import build_client

assert os.environ.get("MB_CRAWLER_CREDS_PATH"), "set MB_CRAWLER_CREDS_PATH=/nonexistent"

# No host literal here on purpose: main() derives it from the live session, and
# /extras ships inside the published sdist, so a constant here would bake one
# school's subdomain into every download. An unresolvable session is a hard
# error instead of a silent fallback.
PATH = "/websocket"

#: Set by ``main()`` from the live session. Empty until then, and deliberately
#: never a literal: an unresolved host must fail loudly rather than probe the
#: wrong school and report a confident negative about this account.
HOST = ""

# Channel names worth trying. The JS bundle showed ProgressChannel,
# PresentationChannel, PresenceChannel, CoreUnitChannel, Chat::RoomChannel and
# AttendanceReportsChannel in use elsewhere in the app; notifications-specific
# names are guesses that this probe exists to confirm or kill.
#
# Calibration (measured against a live .cn session):
#   ProgressChannel, PresenceChannel -> confirm_subscription
#   Chat::RoomChannel                 -> reject_subscription
#   "ZZZDefinitelyNotARealChannelQQQ" -> silence, neither confirm nor reject
# AnyCable therefore REJECTS channels it knows about but will not authorise, and
# answers UNKNOWN channel names with silence. That makes silence a meaningful
# negative: a guessed name that stays silent does not exist. The controls below
# are run first so this calibration holds for the session.
CONTROLS = (
    "ProgressChannel",
    "PresenceChannel",
    "ZZZDefinitelyNotARealChannelQQQ",
)
CHANNELS = (
    "NotificationsChannel",
    "MnnHubChannel",
    "NotificationChannel",
    "MessagesAndNotificationsChannel",
    "NoticesChannel",
    "UnreadNotificationsChannel",
)


def ws_frame(payload: str) -> bytes:
    """Build a masked client->server text frame."""
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])  # FIN + opcode 1 (text)
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bytes(header) + mask + masked


def read_frame(sock: ssl.SSLSocket, timeout: float = 8.0) -> str | None:
    """Read one unmasked server text frame (handles 126/127 lengths, ignores ping)."""
    sock.settimeout(timeout)
    try:
        b0, b1 = sock.recv(1), sock.recv(1)
        if not b0 or not b1:
            return None
        opcode = b0[0] & 0x0F
        ln = b1[0] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", sock.recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", sock.recv(8))[0]
        body = b""
        while len(body) < ln:
            chunk = sock.recv(ln - len(body))
            if not chunk:
                break
            body += chunk
        if opcode == 0x9:  # ping -> ignore
            return read_frame(sock, timeout)
        return body.decode("utf-8", "replace")
    except (socket.timeout, OSError):
        return None


def try_channel(channel: str, token: str, cookie: str, wait: float = 6.0) -> list[str]:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Cookie: _managebac_session={cookie}\r\n"
        f"Origin: https://{HOST}\r\n\r\n"
    )
    ctx = ssl.create_default_context()
    frames: list[str] = []
    with socket.create_connection((HOST, 443), timeout=12) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
            tls.sendall(req.encode())
            tls.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                c = tls.recv(4096)
                if not c:
                    return ["connection closed before handshake"]
                head += c
            if b"101" not in head.split(b"\r\n", 1)[0]:
                return [f"handshake failed: {head.split(chr(13).encode())[0]!r}"]

            ident = json.dumps({"channel": channel})
            tls.sendall(ws_frame(json.dumps({"command": "subscribe", "identifier": ident})))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                frame = read_frame(tls, timeout=deadline - time.monotonic())
                if frame is None:
                    break
                frames.append(frame)
                if '"type":"confirm_subscription"' in frame or "reject" in frame:
                    # keep reading a little longer in case data follows the confirm
                    continue
    return frames


def classify(frames: list[str]) -> str:
    if any("confirm_subscription" in f for f in frames):
        return "CONFIRMED"
    if any("reject_subscription" in f for f in frames):
        return "rejected"
    return "silent (channel does not exist)"


def main() -> int:
    global HOST
    state, client, email = build_client()
    # Derive the host from the live session so this works for any school and
    # does not bake one subdomain into the shipped sdist.
    HOST = client.base.removeprefix("https://").removeprefix("http://").rstrip("/")
    if not HOST:
        # Refuse rather than fall back to a literal: probing the wrong school
        # would produce a confident-looking negative about this account.
        print(
            "could not resolve a host from the live session — re-run `tahuti login`",
            file=sys.stderr,
        )
        return 2
    cookie = client.session.cookies.get("_managebac_session", "")
    print(f"authenticated as {email}")
    print(f"trying ActionCable subscribe on wss://{HOST}{PATH}\n")

    # Run the controls first. Without them a wall of silence would be
    # indistinguishable from a broken frame encoder.
    print("── controls (calibrate what silence means)")
    control_results = {}
    for channel in CONTROLS:
        frames = try_channel(channel, "", cookie)
        verdict = classify(frames)
        control_results[channel] = verdict
        print(f"   {channel:38s} -> {verdict}")
    if control_results.get("ProgressChannel") != "CONFIRMED":
        print("\nABORT: the control channel did not confirm, so every 'silent'")
        print("below would be meaningless. Frame encoding or auth is wrong.")
        return 2
    print()

    any_confirm = False
    for channel in CHANNELS:
        print(f"── {channel}")
        frames = try_channel(channel, "", cookie)
        verdict = classify(frames)
        print(f"   -> {verdict}")
        for f in frames:
            if "ping" not in f and "welcome" not in f:
                print(f"   {f[:300]}")
        if verdict == "CONFIRMED":
            any_confirm = True
        print()

    print("── summary")
    if any_confirm:
        print("A notification channel accepted a subscription. Real push is possible;")
        print("the next step is to watch the frames while a task is actually posted.")
    else:
        print("No guessed notification channel exists. The socket is real and the")
        print("frame encoding is proven by the controls, so this is a genuine")
        print("negative: the channel name has to come from the browser's WS traffic")
        print("with the notifications page open, not from guessing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
