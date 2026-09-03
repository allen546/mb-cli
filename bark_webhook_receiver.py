#!/usr/bin/env python3
"""Lightweight Webhook Receiver Adapter for Bark push notifications.

Receives ManageBac MBEvent webhooks on port 42617 and pushes notifications
to phone and mac via the Bark CLI script.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
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


def format_event_for_bark(payload: dict[str, Any]) -> tuple[str, str, str, int]:
    """Format an MBEvent payload into (title, message, sound, priority).

    Returns:
        tuple of (title, message, sound, priority)
    """
    event = payload.get("event") or payload.get("type") or "notification"
    data = payload.get("data") or {}

    # Extract common fields
    task_title = (
        data.get("title")
        or (data.get("task") or {}).get("title")
        or (data.get("enriched_task") or {}).get("title")
        or "未命名任务"
    )
    class_name = (
        data.get("class_name")
        or (data.get("task") or {}).get("class_name")
        or (data.get("origin") or {}).get("name")
        or ""
    )
    due_date = (
        data.get("due_date")
        or (data.get("task") or {}).get("due_date")
        or (data.get("enriched_task") or {}).get("due_date")
        or ""
    )
    link = (
        data.get("task_url")
        or (data.get("task") or {}).get("link")
        or (data.get("enriched_task") or {}).get("url")
        or ""
    )

    class_tag = f"【{class_name}】" if class_name else ""

    if event == "deadline_approaching":
        threshold = data.get("reminder_threshold") or "即将到期"
        title = f"⏰ ManageBac DDL提醒: {task_title}"
        lines = [
            f"{class_tag}{task_title}",
            f"⚠️ 距离截止时间仅剩: {threshold}",
        ]
        if due_date:
            lines.append(f"📅 截止时间: {due_date}")
        if link:
            lines.append(f"🔗 链接: {link}")
        return title, "\n".join(lines), "alarm", 10

    elif event == "task_created":
        title = f"📝 ManageBac 新作业: {task_title}"
        lines = [f"{class_tag}{task_title}"]
        if due_date:
            lines.append(f"📅 截止时间: {due_date}")
        sender = (data.get("sender") or {}).get("name")
        if sender:
            lines.append(f"👤 教师: {sender}")
        if link:
            lines.append(f"🔗 链接: {link}")
        return title, "\n".join(lines), "bell", 6

    elif event == "task_updated":
        title = f"✏️ ManageBac 作业更新: {task_title}"
        lines = [f"{class_tag}{task_title}"]
        if due_date:
            lines.append(f"📅 截止时间: {due_date}")
        if link:
            lines.append(f"🔗 链接: {link}")
        return title, "\n".join(lines), "bell", 5

    elif event == "assignment_graded":
        grade_letter = data.get("grade_letter") or ""
        grade_score = data.get("grade_score") or data.get("points") or ""
        grade_str = f"{grade_letter} {grade_score}".strip() or "已批改"
        title = f"📊 ManageBac 成绩发布: {task_title}"
        lines = [
            f"{class_tag}{task_title}",
            f"🎯 获得成绩: {grade_str}",
        ]
        if link:
            lines.append(f"🔗 链接: {link}")
        return title, "\n".join(lines), "chime", 7

    elif event == "announcement_created":
        title = f"📢 ManageBac 新公告: {task_title}"
        lines = [f"{class_tag}{task_title}"]
        preview = data.get("body_preview") or data.get("message")
        if preview:
            lines.append(f"\n{preview[:200]}")
        return title, "\n".join(lines), "bell", 5

    elif event == "test_ping":
        title = "🔔 ManageBac Webhook 测试"
        msg = data.get("message") or "ManageBac 实时推送服务连接正常！"
        return title, msg, "bell", 5

    # Fallback for general alert/notification
    title = f"ManageBac 通知: {event}"
    msg = data.get("message") or json.dumps(data, ensure_ascii=False, indent=2)
    return title, str(msg), "bell", 5


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
            log.info("Dispatching to Bark: title=%r, sound=%r, priority=%d", title, sound, priority)
            ok = pusher.push(title, message, sound=sound, priority=priority)

            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": ok, "event": event_name}) + "\n"
            self.wfile.write(resp.encode("utf-8"))

        def log_message(self, format, *args):
            # Suppress default BaseHTTPRequestHandler access log line noise
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
