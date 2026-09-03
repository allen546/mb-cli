#!/usr/bin/env python3
"""Lightweight Webhook Receiver Adapter for Bark push notifications.

Receives ManageBac MBEvent webhooks on port 42617 and pushes compact
notifications (max 4 short lines) to phone and mac via the Bark CLI script.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bark_webhook_receiver")

DEFAULT_BARK_BIN = "/srv/data/tools/bark"
DEFAULT_BARK_CONFIG = "/srv/data/tools/bark_config.json"
DEFAULT_PORT = 42617
DEFAULT_HOST = "127.0.0.1"
MAX_LINE_LEN = 20


def truncate_line(s: str | None, max_len: int = MAX_LINE_LEN) -> str:
    """Clean and truncate a string to maximum length."""
    if not s:
        return ""
    cleaned = re.sub(r"\s+", " ", str(s)).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 2] + ".."


def format_compact_date(date_str: str | None) -> str:
    """Format diverse ManageBac date strings to compact MM-DD HH:MM."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    for prefix in ("due:", "when:", "due", "when", "when :", "due :"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :].strip()

    # ISO format: 2026-09-10T08:25:00 or 2026-09-10 08:25:00
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"

    # English format: September 10, 2026 at 9:10 AM or Sep 2 at 2:20 PM
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    m2 = re.search(
        r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),?\s*(?:\d{4})?\s*(?:at\s*)?(\d{1,2}):(\d{2})\s*(AM|PM)?",
        s,
        re.IGNORECASE,
    )
    if m2:
        mon_str = m2.group(1).lower()
        mon = months.get(mon_str, "01")
        day = f"{int(m2.group(2)):02d}"
        hr = int(m2.group(3))
        minute = m2.group(4)
        ampm = (m2.group(5) or "").upper()
        if ampm == "PM" and hr < 12:
            hr += 12
        elif ampm == "AM" and hr == 12:
            hr = 0
        return f"{mon}-{day} {hr:02d}:{minute}"

    # Fallback to truncated text
    return s[:14]


def format_event_for_bark(payload: dict[str, Any]) -> tuple[str, str, str, int]:
    """Format an MBEvent payload into (title, message, sound, priority).

    Enforces:
    - Maximum 4 lines of body
    - Maximum line length equal to the date line (~20 chars)
    """
    event = payload.get("event") or payload.get("type") or "notification"
    data = payload.get("data") or {}

    raw_title = (
        data.get("title")
        or (data.get("task") or {}).get("title")
        or (data.get("enriched_task") or {}).get("title")
        or ""
    )
    raw_class = (
        data.get("class_name")
        or (data.get("task") or {}).get("class_name")
        or (data.get("origin") or {}).get("name")
        or ""
    )
    raw_due = (
        data.get("due_date")
        or (data.get("task") or {}).get("due_date")
        or (data.get("enriched_task") or {}).get("due_date")
        or ""
    )
    date_formatted = format_compact_date(raw_due)
    if not date_formatted:
        date_formatted = datetime.now().strftime("%m-%d %H:%M")

    # Clean class name (strip noisy boilerplate like "2026-2027 (Grade 10)")
    clean_class = re.sub(r"\b20\d{2}-20\d{2}\b", "", raw_class)
    clean_class = re.sub(r"\(Grade \d+\)", "", clean_class)
    clean_class = re.sub(r"\s+", " ", clean_class).strip()
    if not clean_class:
        clean_class = "ManageBac"

    line1 = truncate_line(clean_class, MAX_LINE_LEN)
    line2 = truncate_line(raw_title or "新消息", MAX_LINE_LEN)

    if event == "deadline_approaching":
        threshold = data.get("reminder_threshold") or "到期"
        title = truncate_line(f"⏰ DDL: {raw_title}", 22)
        line3 = truncate_line(f"剩余: {threshold}", MAX_LINE_LEN)
        line4 = f"截止: {date_formatted}"
        sound = "alarm"
        priority = 10

    elif event == "task_created":
        title = truncate_line(f"📝 新作业: {raw_title}", 22)
        teacher = (data.get("sender") or {}).get("name") or ""
        line3 = truncate_line(f"教师: {teacher}" if teacher else "新布置作业", MAX_LINE_LEN)
        line4 = f"截止: {date_formatted}" if raw_due else f"发布: {date_formatted}"
        sound = "bell"
        priority = 6

    elif event == "task_updated":
        title = truncate_line(f"✏️ 更新: {raw_title}", 22)
        status = (data.get("enriched_task") or {}).get("status")
        status_str = "已提交" if status == "submitted" else "未提交"
        line3 = truncate_line(f"状态: {status_str}", MAX_LINE_LEN)
        line4 = f"截止: {date_formatted}" if raw_due else f"时间: {date_formatted}"
        sound = "bell"
        priority = 5

    elif event == "assignment_graded":
        title = truncate_line(f"📊 成绩: {raw_title}", 22)
        grade_letter = data.get("grade_letter") or ""
        grade_score = data.get("grade_score") or data.get("points") or ""
        grade_str = f"{grade_letter} {grade_score}".strip() or "已批改"
        line3 = truncate_line(f"得分: {grade_str}", MAX_LINE_LEN)
        line4 = f"时间: {date_formatted}"
        sound = "chime"
        priority = 7

    elif event == "announcement_created":
        title = truncate_line(f"📢 公告: {raw_title}", 22)
        author = (data.get("sender") or {}).get("name") or ""
        line3 = truncate_line(f"发布: {author}" if author else "新公告", MAX_LINE_LEN)
        line4 = f"时间: {date_formatted}"
        sound = "bell"
        priority = 5

    elif event == "test_ping":
        title = "🔔 ManageBac 测试"
        line1 = "实时推送测试"
        line2 = "设备: Mac & iPhone"
        line3 = "服务状态: 正常"
        line4 = f"时间: {date_formatted}"
        sound = "bell"
        priority = 5

    else:
        title = truncate_line(f"ManageBac: {raw_title or event}", 22)
        preview = data.get("body_preview") or data.get("message") or ""
        line3 = truncate_line(preview or "新通知", MAX_LINE_LEN)
        line4 = f"时间: {date_formatted}"
        sound = "bell"
        priority = 5

    # Strictly assemble at most 4 lines
    lines = [line1, line2, line3, line4]
    final_lines = [l for l in lines if l][:4]
    message = "\n".join(final_lines)

    return title, message, sound, priority


class BarkPusher:
    def __init__(self, bark_bin: str = DEFAULT_BARK_BIN):
        self.bark_bin = bark_bin

    def push(self, title: str, message: str, sound: str = "bell", priority: int = 5) -> bool:
        if not os.path.exists(self.bark_bin):
            log.error("Bark binary not found at %s", self.bark_bin)
            return False

        cmd = [
            self.bark_bin,
            "-t",
            title,
            "-s",
            sound,
            "-p",
            str(priority),
            message,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                log.info("Bark pushed successfully: %s", res.stdout.strip())
                return True
            else:
                log.error("Bark push failed (code %d): %s", res.returncode, res.stderr.strip())
                return False
        except Exception as e:
            log.error("Error executing Bark command %s: %s", cmd, e)
            return False


def make_request_handler(pusher: BarkPusher):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/health", "/ping", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok","service":"bark_webhook_receiver"}\n')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
            except Exception as e:
                log.warning("Invalid JSON payload received: %s", e)
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"invalid_json"}\n')
                return

            event_name = self.headers.get("X-MB-Event") or payload.get("event", "unknown")
            log.info("Received event: %s", event_name)

            title, message, sound, priority = format_event_for_bark(payload)
            log.info("Dispatching to Bark:\nTitle: %r\nMessage:\n%s\nSound: %r, Priority: %d", title, message, sound, priority)
            ok = pusher.push(title, message, sound=sound, priority=priority)

            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": ok, "event": event_name}) + "\n"
            self.wfile.write(resp.encode("utf-8"))

        def log_message(self, format, *args):
            pass

    return WebhookHandler


def main():
    parser = argparse.ArgumentParser(description="ManageBac Bark Webhook Receiver")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on (default: 42617)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--bark-bin", default=DEFAULT_BARK_BIN, help="Path to bark CLI script")
    args = parser.parse_args()

    pusher = BarkPusher(bark_bin=args.bark_bin)
    handler_cls = make_request_handler(pusher)

    server = HTTPServer((args.host, args.port), handler_cls)
    log.info("Starting Bark Webhook Receiver on http://%s:%d/webhook ...", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down Bark Webhook Receiver...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
