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

DEFAULT_BARK_BIN = "/mnt/pi-data/tools/bark"
DEFAULT_PORT = 42617
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ALIASES_PATH = Path.home() / ".config" / "managebac" / "course_aliases.json"
MAX_COURSE_LEN = 40
MAX_TASK_LEN = 80
MAX_META_LEN = 40
MAX_TITLE_LEN = 50

_aliases_cache: dict[str, str] = {}
_aliases_mtime: float = -1.0
_aliases_path_cached: Path | None = None


def load_course_aliases(path: Path | str | None = None) -> dict[str, str]:
    """Load course alias mapping from JSON file with mtime caching.

    Returns an empty dict if the file is missing or invalid.
    """
    global _aliases_cache, _aliases_mtime, _aliases_path_cached
    target_path = Path(path).expanduser() if path else DEFAULT_ALIASES_PATH
    try:
        if not target_path.exists():
            return {}
        mtime = target_path.stat().st_mtime
        if target_path == _aliases_path_cached and mtime == _aliases_mtime:
            return _aliases_cache
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _aliases_cache = {str(k): str(v) for k, v in data.items()}
            _aliases_mtime = mtime
            _aliases_path_cached = target_path
            return _aliases_cache
        log.warning("Aliases file at %s is not a JSON object", target_path)
        return {}
    except Exception as e:
        log.warning("Failed to load course aliases from %s: %s", target_path, e)
        return {}


def truncate(s: str | None, max_len: int = MAX_COURSE_LEN) -> str:
    """Safely bound line length to avoid runaway multi-line wrapping while keeping full context."""
    if not s:
        return ""
    cleaned = re.sub(r"\s+", " ", str(s)).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 2] + ".."


def resolve_course_name(raw_name: str | None, aliases: dict[str, str] | None = None) -> str:
    """Resolve course name using exact case-sensitive match against user aliases.

    Zero autocleaning or heuristic manipulation is performed.
    """
    if not raw_name:
        return "ManageBac"
    trimmed = str(raw_name).strip()
    if aliases and trimmed in aliases:
        name = aliases[trimmed]
    else:
        name = trimmed
    return truncate(name or "ManageBac", MAX_COURSE_LEN)


def clean_class_name(raw_name: str | None, aliases: dict[str, str] | None = None) -> str:
    """Backward compatibility alias for resolve_course_name."""
    return resolve_course_name(raw_name, aliases=aliases)


def clean_task_title(raw_title: str | None, max_len: int = MAX_TASK_LEN) -> str:
    """Extract full clean task name without 'New Task:' or 'Updated Task:' prefixes."""
    if not raw_title:
        return "未命名作业"
    s = str(raw_title).strip()
    s = re.sub(r"^(?:New\s+Task|Updated\s+Task|Task):\s*", "", s, flags=re.I).strip()
    return truncate(s or "未命名作业", max_len)


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


def has_released_grade(data: dict[str, Any]) -> bool:
    """Return True if grade/score has been released for the task."""
    if not isinstance(data, dict):
        return False
    enriched = data.get("enriched_task") or {}
    task_obj = data.get("task") or {}

    status = str(
        data.get("status")
        or enriched.get("status")
        or task_obj.get("status")
        or ""
    ).strip().lower()
    if status == "graded":
        return True

    letter = str(
        data.get("grade_letter")
        or enriched.get("grade_letter")
        or task_obj.get("grade_letter")
        or ""
    ).strip()
    score = str(
        data.get("grade_score")
        or enriched.get("grade_score")
        or task_obj.get("grade_score")
        or data.get("points")
        or ""
    ).strip()

    if letter == "-":
        letter = ""
    if score == "-":
        score = ""

    non_grade_terms = (
        "submitted",
        "pending",
        "not-submitted",
        "not submitted",
        "not assessed yet",
        "not assessed",
        "ungraded",
    )
    if score.lower() in non_grade_terms:
        score = ""
    if letter.lower() in non_grade_terms:
        letter = ""

    labels = [
        str(l).lower()
        for l in (data.get("labels") or [])
        + (enriched.get("labels") or [])
        + (task_obj.get("labels") or [])
    ]

    if letter.lower() in ("n/a", "not applicable", "exempt", "excused") or any(
        l in ("exempt", "excused") for l in labels
    ):
        return True

    if "not assessed yet" in letter.lower() or "not assessed yet" in labels:
        if not (score and not re.match(r"^\s*0\s*/", score)):
            return False

    if score and re.match(r"^\s*0\s*/", score) and not letter:
        return False

    has_score = bool(score)
    has_letter = bool(letter and letter.lower() not in ("not assessed", "not assessed yet"))
    if has_score or has_letter or any(l == "graded" for l in labels):
        return True

    return False


def format_event_for_bark(
    payload: dict[str, Any],
    aliases: dict[str, str] | None = None,
) -> tuple[str, str, str, int, str]:
    """Format an MBEvent payload into (title, message, sound, priority, url).

    Guarantees:
    - Clean English alert titles without markdown formatting
    - Exactly 3 logical fields budgeted for at most 4 visual lines:
        * Field 1: 课程: {course} (1 visual line, exact alias applied without autocleaning)
        * Field 2: 作业: {task} (up to 2 visual lines)
        * Field 3: 截止/得分/状态 (1 visual line)
    - Zero teacher noise
    - Direct assignment URL for instant click-through
    """
    event = payload.get("event") or payload.get("type") or "notification"
    data = payload.get("data") or {}

    # If a score is released along with the task itself, show scores released banner instead of new task
    if (
        event in ("task_created", "new_task", "task_updated", "updated_task", "new_upcoming")
        and has_released_grade(data)
    ):
        event = "task_graded"

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
    if not raw_url:
        c_id = data.get("class_id") or (data.get("task") or {}).get("class_id")
        t_id = data.get("task_id") or (data.get("task") or {}).get("id") or (data.get("task") or {}).get("task_id")
        if c_id and t_id:
            raw_url = f"https://beijing101.managebac.cn/student/classes/{c_id}/core_tasks/{t_id}"

    clean_cls = resolve_course_name(raw_class, aliases=aliases)
    clean_tsk = clean_task_title(raw_title, max_len=MAX_TASK_LEN)

    raw_class_clean = re.sub(r"\(.*?(?:\)|$)", "", raw_class).strip().lower().rstrip(". ")
    clean_tsk_clean = re.sub(r"\(.*?(?:\)|$)", "", clean_tsk).strip().lower()
    is_class_name_match = (
        clean_tsk.lower() == raw_class.lower()
        or clean_tsk.lower() == clean_cls.lower()
        or (len(raw_class_clean) >= 5 and raw_class_clean in clean_tsk_clean)
        or (len(raw_class_clean) >= 5 and clean_tsk_clean in raw_class_clean)
    )

    # Fallback if clean_tsk matches class name or is generic
    if (
        is_class_name_match
        or clean_tsk in ("未命名作业", "Updated Task", "New Task")
    ):
        enriched_title = (data.get("enriched_task") or {}).get("title")
        if enriched_title and enriched_title.strip().lower() not in (raw_class.lower(), clean_cls.lower()):
            clean_tsk = clean_task_title(enriched_title, max_len=MAX_TASK_LEN)
        else:
            body_str = data.get("body") or ""
            body_prev = data.get("body_preview") or ""
            extracted = None
            if body_str:
                m = re.search(
                    r"(?:added a new|updated the|created a|added the)\s+Task\s+<strong[^>]*>(.*?)</strong>",
                    body_str,
                    re.IGNORECASE,
                )
                if not m:
                    m = re.search(r"Task\s+<strong[^>]*>(.*?)</strong>", body_str, re.IGNORECASE)
                if m:
                    extracted = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if not extracted and body_prev:
                m = re.search(
                    r"(?:added a new|updated the|created a|added the)\s+Task\s+(.*?)\s+in\s+",
                    body_prev,
                    re.IGNORECASE,
                )
                if not m:
                    m = re.search(r"Task\s+(.*?)\s+in\s+", body_prev, re.IGNORECASE)
                if m:
                    extracted = m.group(1).strip()

            if extracted and extracted.lower() not in (raw_class.lower(), clean_cls.lower()):
                clean_tsk = clean_task_title(extracted, max_len=MAX_TASK_LEN)

    due_line = format_relative_due_date(raw_due)

    if event in ("task_created", "new_task"):
        title = "📝 New Task"
        field1 = f"课程: {clean_cls}"
        field2 = f"作业: {clean_tsk}"
        field3 = due_line or f"发布: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 6

    elif event in ("task_updated", "updated_task"):
        title = "✏️ Updated Task"
        field1 = f"课程: {clean_cls}"
        field2 = f"作业: {clean_tsk}"
        status = (data.get("enriched_task") or {}).get("status")
        if status == "submitted":
            field3 = f"{due_line} (已提交)" if due_line else "状态: 已提交"
        elif status == "not-submitted":
            field3 = f"{due_line} (未提交)" if due_line else "状态: 未提交"
        else:
            field3 = due_line or f"更新: {datetime.now().strftime('%m-%d %H:%M')}"
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
        title = "⏰ DDL Warning"
        field1 = f"课程: {clean_cls}"
        field2 = f"作业: {clean_tsk}"

        mins_left = data.get("time_remaining_minutes")
        if mins_left is not None:
            mins_val = float(mins_left)
            total_mins = max(0, int(round(mins_val)))
            hrs, mins = divmod(total_mins, 60)
            if hrs > 0 and mins > 0:
                time_str = f"{hrs}小时{mins}分"
            elif hrs > 0:
                time_str = f"{hrs}小时"
            else:
                time_str = f"{mins}分钟"

            prefix = "仅剩 " if total_mins <= 60 else "还剩 "
            countdown = f"{prefix}{time_str}"
        else:
            prefix = "仅剩 " if threshold in ("15m", "1h") else "还剩 "
            countdown = f"{prefix}{friendly_th}"

        # Clean base due date without redundant (今晚)/(下午)/(明天) when countdown is present
        dt = parse_datetime(raw_due)
        if dt:
            field3 = f"截止: {dt.strftime('%m-%d %H:%M')} ({countdown})"
        elif due_line:
            field3 = f"{due_line} ({countdown})"
        else:
            field3 = f"截止: ({countdown})"
        sound = "alarm"
        priority = 10

    elif event in ("assignment_graded", "grade_posted", "task_graded"):
        title = "📊 Grade Posted"
        field1 = f"课程: {clean_cls}"
        field2 = f"作业: {clean_tsk}"
        grade_letter = (
            data.get("grade_letter")
            or (data.get("enriched_task") or {}).get("grade_letter")
            or (data.get("task") or {}).get("grade_letter")
            or ""
        )
        grade_score = (
            data.get("grade_score")
            or (data.get("enriched_task") or {}).get("grade_score")
            or (data.get("task") or {}).get("grade_score")
            or data.get("points")
            or ""
        )
        if str(grade_letter).upper() in ("N/A", "NOT APPLICABLE", "EXEMPT", "EXCUSED"):
            grade_str = "N/A"
        else:
            grade_str = f"{grade_letter} {grade_score}".strip() or "已批改"
        field3 = f"得分: {grade_str}"
        sound = "chime"
        priority = 7

    elif event in ("file_uploaded", "new_file_uploaded"):
        title = "📁 File Uploaded"
        field1 = f"课程: {clean_cls}"
        preview = data.get("body_preview") or data.get("title") or ""
        m_file = re.search(r"named\s+([^\s]+\.\w+)", preview)
        filename = m_file.group(1) if m_file else clean_tsk
        field2 = f"课件: {filename}"
        field3 = f"上传: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    elif event in ("announcement_created", "new_announcement"):
        title = "📢 Class Announcement"
        field1 = f"课程: {clean_cls}"
        field2 = f"主题: {clean_tsk}"
        field3 = f"发布: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    elif event == "test_ping":
        title = "🔔 Test Notification"
        field1 = "通道: 实时推送正常"
        field2 = "设备: Mac & iPhone"
        field3 = f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    else:
        title = "ManageBac Notification"
        field1 = f"课程: {clean_cls}"
        field2 = f"内容: {clean_tsk}"
        field3 = due_line or f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
        sound = "bell"
        priority = 5

    # Assemble at most 3 logical fields, guaranteeing max 4 visual lines
    fields = [
        truncate(field1, MAX_COURSE_LEN),
        truncate(field2, MAX_TASK_LEN),
        truncate(field3, MAX_META_LEN),
    ]
    message = "\n".join(f for f in fields if f)

    return title, message, sound, priority, raw_url


def is_task_event_suppressed(payload: dict[str, Any]) -> bool:
    """Return True if the event is for a task that is already submitted or graded."""
    event_name = payload.get("event") or payload.get("type") or ""
    if event_name not in ("deadline_approaching", "task_updated", "updated_task"):
        return False

    data = payload.get("data") or {}
    enriched = data.get("enriched_task") or {}
    task_obj = data.get("task") or {}
    status = str(data.get("status") or enriched.get("status") or task_obj.get("status") or "").lower()
    if status == "submitted":
        return True

    labels = [str(l).lower() for l in (data.get("labels") or []) + (enriched.get("labels") or []) + (task_obj.get("labels") or [])]
    if any("submitted" in l and "not" not in l and "un" not in l for l in labels):
        return True

    if has_released_grade(data):
        return True

    return False


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


def make_request_handler(pusher: BarkPusher, aliases_path: Path | str | None = None):
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

            if is_task_event_suppressed(payload):
                log.info("Suppressed event %s for task: already submitted or graded", event_name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = json.dumps({"ok": True, "suppressed": True, "event": event_name}) + "\n"
                self.wfile.write(resp.encode("utf-8"))
                return

            aliases = load_course_aliases(aliases_path)
            title, message, sound, priority, url = format_event_for_bark(payload, aliases=aliases)
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
    parser.add_argument(
        "--course-aliases",
        default=str(DEFAULT_ALIASES_PATH),
        help="Path to course aliases JSON file (default: ~/.config/managebac/course_aliases.json)",
    )
    args = parser.parse_args()

    pusher = BarkPusher(bark_bin=args.bark_bin)
    handler_cls = make_request_handler(pusher, aliases_path=args.course_aliases)

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
