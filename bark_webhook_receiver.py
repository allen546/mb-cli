#!/usr/bin/env python3
"""Lightweight Webhook Receiver Adapter for Bark push notifications.

Receives ManageBac MBEvent webhooks on port 42617 and pushes complete,
informative 4-line notifications to phone and mac via the Bark CLI script.
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
DEFAULT_PORT = 42617
DEFAULT_HOST = "127.0.0.1"
MAX_LINE_LEN = 60
MAX_TITLE_LEN = 50


def truncate(s: str | None, max_len: int = MAX_LINE_LEN) -> str:
    """Safely bound line length to avoid runaway multi-line wrapping while keeping full context."""
    if not s:
        return ""
    cleaned = re.sub(r"\s+", " ", str(s)).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 2] + ".."


def clean_class_name(raw_name: str | None) -> str:
    """Clean verbose ManageBac class names into clear, readable course names."""
    if not raw_name:
        return "ManageBac"
    s = str(raw_name).strip()
    # Normalize repeated AP prefixes: e.g. "AP AP—Calculus BC" -> "AP Calculus BC"
    s = re.sub(r"^(?:AP\s+)+AP[—\- ]*", "AP ", s)
    # Remove metadata noise
    s = re.sub(r"\b20\d{2}-20\d{2}\b", "", s)
    s = re.sub(r"\(Grade \d+\)", "", s)
    s = re.sub(r"\((?:Grade|AP)[^)]*\)", "", s)
    s = re.sub(r"\bCLASS\s+\d+\b", "", s, flags=re.I)
    s = re.sub(r"\b(?:BLUE|YELLOW|RED|GREEN|E101|E102)\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -—:")

    return truncate(s or "ManageBac", MAX_LINE_LEN)


def clean_task_title(raw_title: str | None) -> str:
    """Extract full clean task name without 'New Task:' or 'Updated Task:' prefixes."""
    if not raw_title:
        return "未命名作业"
    s = str(raw_title).strip()
    s = re.sub(r"^(?:New\s+Task|Updated\s+Task|Task):\s*", "", s, flags=re.I).strip()
    return truncate(s or "未命名作业", MAX_LINE_LEN)


def clean_teacher_name(raw_name: str | None) -> str:
    """Clean teacher name, displaying Chinese and English names cleanly."""
    if not raw_name:
        return ""
    s = str(raw_name).strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        cn = next((p for p in parts if re.search(r"[\u4e00-\u9fa5]", p)), "")
        en = next((p for p in parts if not re.search(r"[\u4e00-\u9fa5]", p)), "")
        if cn and en:
            m = re.search(r"\(([^)]+)\)\s*([A-Za-z]+)", en)
            en_short = f"{m.group(1)} {m.group(2)}" if m else en
            return f"{cn} ({en_short})"
        return cn or en
    s = re.sub(r"\s+", " ", s).strip()
    return truncate(s, 30)


def parse_datetime(s: str | None) -> datetime | None:
    """Parse various ManageBac date formats."""
    if not s:
        return None
    s = str(s).strip()
    for prefix in ("due:", "when:", "due", "when", "when :", "due :"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :].strip()

    # ISO format
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", s)
    if m:
        try:
            return datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
            )
        except Exception:
            pass

    # English format: September 10, 2026 at 9:10 AM
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m2 = re.search(
        r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),?\s*(?:(\d{4})\s*)?(?:at\s*)?(\d{1,2}):(\d{2})\s*(AM|PM)?",
        s,
        re.IGNORECASE,
    )
    if m2:
        try:
            year = int(m2.group(3)) if m2.group(3) else datetime.now().year
            mon = months.get(m2.group(1).lower(), 1)
            day = int(m2.group(2))
            hr = int(m2.group(4))
            minute = int(m2.group(5))
            ampm = (m2.group(6) or "").upper()
            if ampm == "PM" and hr < 12:
                hr += 12
            elif ampm == "AM" and hr == 12:
                hr = 0
            return datetime(year, mon, day, hr, minute)
        except Exception:
            pass

    return None


def format_relative_due_date(raw_due: str | None, now: datetime | None = None) -> str:
    """Format due date with relative day context (今晚, 明天, etc.)."""
    if not raw_due:
        return ""
    if now is None:
        now = datetime.now()

    dt = parse_datetime(raw_due)
    if not dt:
        clean = re.sub(r"^(?:due|when):\s*", "", str(raw_due), flags=re.I).strip()
        return f"截止: {clean[:16]}"

    diff_days = (dt.date() - now.date()).days
    time_str = dt.strftime("%H:%M")

    if diff_days == 0:
        hr = dt.hour
        period = "今晚" if hr >= 18 else ("下午" if hr >= 12 else "上午")
        return f"截止: {dt.strftime('%m-%d')} {time_str} ({period})"
    elif diff_days == 1:
        return f"截止: {dt.strftime('%m-%d')} {time_str} (明天)"
    elif diff_days == 2:
        return f"截止: {dt.strftime('%m-%d')} {time_str} (后天)"
    elif diff_days < 0:
        return f"截止: {dt.strftime('%m-%d %H:%M')} (已超时)"
    else:
        return f"截止: {dt.strftime('%m-%d %H:%M')}"


def format_event_for_bark(payload: dict[str, Any]) -> tuple[str, str, str, int, str]:
    """Format an MBEvent payload into (title, message, sound, priority, url).

    Guarantees:
    - Maximum 4 lines of showing space
    - Full, actionable information without premature ellipsis
    - Direct assignment URL for instant click-through
    """
    event = payload.get("event") or payload.get("type") or "notification"
    data = payload.get("data") or {}

    raw_title = (
        data.get("task_title")
        or data.get("title")
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
    raw_url = (
        data.get("url")
        or data.get("task_url")
        or (data.get("task") or {}).get("link")
        or (data.get("enriched_task") or {}).get("url")
        or ""
    )

    clean_cls = clean_class_name(raw_class)
    clean_tsk = clean_task_title(raw_title)
    teacher = clean_teacher_name((data.get("sender") or {}).get("name"))
    due_line = format_relative_due_date(raw_due)

    if event in ("task_created", "new_task"):
        title = truncate(f"📝 {clean_cls}: {clean_tsk}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"作业: {clean_tsk}"
        line3 = f"教师: {teacher}" if teacher else "新布置作业"
        line4 = due_line or f"发布: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 6

    elif event in ("task_updated", "updated_task"):
        title = truncate(f"✏️ {clean_cls}: {clean_tsk}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"作业: {clean_tsk}"
        status = (data.get("enriched_task") or {}).get("status")
        if status == "submitted":
            line3 = "状态: 已提交"
        elif status == "not-submitted":
            line3 = "状态: 未提交"
        elif teacher:
            line3 = f"教师: {teacher}"
        else:
            line3 = "作业内容已更新"
        line4 = due_line or f"更新: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    elif event == "deadline_approaching":
        threshold = data.get("reminder_threshold") or "即将到期"
        threshold_names = {
            "24h": "24小时",
            "6h": "6小时",
            "1h": "1小时",
            "15m": "15分钟",
        }
        friendly_th = threshold_names.get(threshold, threshold)
        title = truncate(f"⏰ DDL提醒: {clean_tsk}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"作业: {clean_tsk}"

        mins_left = data.get("time_remaining_minutes")
        if mins_left is not None:
            mins_val = float(mins_left)
            hrs = int(mins_val // 60)
            mins = int(round(mins_val % 60))
            if hrs > 0 and mins > 0:
                time_str = f"{hrs}小时{mins}分"
            elif hrs > 0:
                time_str = f"{hrs}小时"
            else:
                time_str = f"{mins}分钟"

            if mins_val <= 15:
                line3 = f"🚨 倒计时: 距离截止仅剩 {time_str}"
            elif mins_val <= 60:
                line3 = f"⚠️ 紧急: 距离截止仅剩 {time_str}"
            else:
                line3 = f"⏰ 提醒: 距离截止还剩 {time_str}"
        else:
            if threshold == "15m":
                line3 = f"🚨 倒计时: 距离截止仅剩 {friendly_th}"
            elif threshold == "1h":
                line3 = f"⚠️ 紧急: 距离截止仅剩 {friendly_th}"
            else:
                line3 = f"⏰ 提醒: 距离截止还剩 {friendly_th}"

        line4 = due_line
        sound = "alarm"
        priority = 10

    elif event in ("assignment_graded", "grade_posted"):
        title = truncate(f"📊 成绩发布: {clean_tsk}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"作业: {clean_tsk}"
        grade_letter = data.get("grade_letter") or ""
        grade_score = data.get("grade_score") or data.get("points") or ""
        grade_str = f"{grade_letter} {grade_score}".strip() or "已批改"
        line3 = f"得分: {grade_str}"
        line4 = f"发布: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "chime"
        priority = 7

    elif event in ("file_uploaded", "new_file_uploaded"):
        title = truncate(f"📁 课件上传: {clean_cls}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        preview = data.get("body_preview") or data.get("title") or ""
        m_file = re.search(r"named\s+([^\s]+\.\w+)", preview)
        filename = m_file.group(1) if m_file else clean_tsk
        line2 = f"课件: {filename}"
        line3 = f"教师: {teacher}" if teacher else "新课件附件"
        line4 = f"上传: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    elif event in ("announcement_created", "new_announcement"):
        title = truncate(f"📢 班级公告: {clean_cls}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"主题: {clean_tsk}"
        line3 = f"发布: {teacher}" if teacher else "班级新公告"
        line4 = f"发布: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    elif event == "test_ping":
        title = "🔔 ManageBac 测试通知"
        line1 = "通道: 实时推送正常"
        line2 = "设备: Mac & iPhone"
        line3 = "状态: 监听端口 42617"
        line4 = f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    else:
        title = truncate(f"ManageBac: {clean_cls}", MAX_TITLE_LEN)
        line1 = f"课程: {clean_cls}"
        line2 = f"内容: {clean_tsk}"
        line3 = f"教师: {teacher}" if teacher else "新通知"
        line4 = due_line or f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    # Assemble at most 4 lines
    lines = [
        truncate(line1, MAX_LINE_LEN),
        truncate(line2, MAX_LINE_LEN),
        truncate(line3, MAX_LINE_LEN),
        truncate(line4, MAX_LINE_LEN),
    ]
    final_lines = [l for l in lines if l][:4]
    message = "\n".join(final_lines)

    return title, message, sound, priority, raw_url


class BarkPusher:
    def __init__(self, bark_bin: str = DEFAULT_BARK_BIN):
        self.bark_bin = bark_bin

    def push(
        self,
        title: str,
        message: str,
        sound: str = "bell",
        priority: int = 5,
        url: str = "",
    ) -> bool:
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
        ]
        if url:
            cmd.extend(["-u", url])
        cmd.append(message)

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

            title, message, sound, priority, url = format_event_for_bark(payload)
            log.info(
                "Dispatching to Bark:\nTitle: %r\nMessage:\n%s\nSound: %r, Priority: %d, URL: %r",
                title,
                message,
                sound,
                priority,
                url,
            )
            ok = pusher.push(title, message, sound=sound, priority=priority, url=url)

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
