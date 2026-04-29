"""CLI output formatting and payload construction."""

from __future__ import annotations

import json
import sys
from textwrap import indent


def resolve_format(requested_format: str | None) -> str:
    if requested_format:
        return requested_format
    return "pretty" if sys.stdout.isatty() else "json"


def render_pretty(payload: dict) -> str:
    if not payload.get("ok"):
        error = payload.get("error", {})
        return (
            f"ERROR [{error.get('code', 'unknown')}]: "
            f"{error.get('message', 'Unknown error')}"
        )

    command = payload.get("command", "unknown")
    profile = payload.get("profile", "default")
    data = payload.get("data", {})

    if command == "login":
        return (
            "Login successful\n"
            f"  profile: {profile}\n"
            f"  school: {data.get('school')}\n"
            f"  domain: {data.get('domain')}\n"
            f"  email: {data.get('email')}\n"
            f"  base_url: {data.get('base_url')}\n"
            f"  auth_method: {data.get('auth_method')}"
        )

    if command == "logout":
        return (
            "Logout complete\n"
            f"  profile: {profile}\n"
            f"  all_profiles: {data.get('all_profiles')}"
        )

    if command == "list":
        meta = data.get("meta", {})
        summary = data.get("summary", {})
        tasks = data.get("tasks", {})
        lines = [
            "Task list",
            f"  profile: {profile}",
            f"  student: {meta.get('student_name')}",
            f"  school: {meta.get('school')}",
            f"  view: {meta.get('view')}",
            f"  subject_filter: {meta.get('subject_filter') or '-'}",
            f"  details: {meta.get('details')}",
            f"  upcoming: {summary.get('upcoming_count', 0)}",
            f"  past: {summary.get('past_count', 0)}",
            f"  overdue: {summary.get('overdue_count', 0)}",
            f"  total: {summary.get('total_count', 0)}",
        ]
        for section in ("upcoming", "past", "overdue"):
            section_tasks = tasks.get(section, [])
            if not section_tasks:
                continue
            lines.append(f"\n[{section}]")
            for task in section_tasks:
                grade = task.get("grade_score") or "-"
                lines.append(
                    f"- {task.get('id')} | {task.get('title')} | "
                    f"{task.get('class_name')} | {task.get('due_date')} | {grade}"
                )
        return "\n".join(lines)

    if command == "view":
        task = data.get("task", {}) or {}
        detail = data.get("detail", {}) or {}
        lines = [
            "Task detail",
            f"  profile: {profile}",
            f"  id: {task.get('id')}",
            f"  title: {task.get('title')}",
            f"  class: {task.get('class_name')}",
            f"  due: {task.get('due_date')}",
            f"  grade: {task.get('grade_score')}",
            f"  link: {task.get('link')}",
        ]
        if detail.get("description"):
            lines.append("\n[description]")
            lines.append(indent(detail["description"], "  "))
        if detail.get("comments"):
            lines.append("\n[comments]")
            for idx, comment in enumerate(detail["comments"], start=1):
                lines.append(f"  ({idx})")
                lines.append(indent(comment, "    "))
        if detail.get("attachments"):
            lines.append("\n[attachments]")
            for attachment in detail["attachments"]:
                lines.append(
                    f"- {attachment.get('source')}: {attachment.get('name')} "
                    f"-> {attachment.get('url')}"
                )
        return "\n".join(lines)

    if command == "submit":
        return (
            "File submitted\n"
            f"  profile: {profile}\n"
            f"  filename: {data.get('filename')}\n"
            f"  task_url: {data.get('task_url')}"
        )

    if command == "notifications":
        stats = data.get("stats", {})
        items = data.get("items", [])
        meta = data.get("meta", {})
        lines = [
            "Notifications",
            f"  profile: {profile}",
            f"  unread: {stats.get('unread_messages', '?')}",
            f"  page: {meta.get('page', '?')}/{meta.get('total_pages', '?')}",
            f"  total: {meta.get('total', '?')}",
        ]
        for item in items:
            read_flag = " " if item.get("is_read") else "*"
            title = item.get("title", "?")
            created = (item.get("created_at") or "")[:16]
            lines.append(f"  {read_flag} [{item.get('id')}] {title}  ({created})")
        if not items:
            lines.append("  (none)")
        return "\n".join(lines)

    if command == "notifications.mutate":
        action = data.get("action", "?")
        nid = data.get("notification_id", "?")
        ok = data.get("ok", False)
        return f"Notification {action}\n  id: {nid}\n  ok: {ok}"

    if command == "calendar":
        events = data.get("events", [])
        lines = [
            "Calendar events",
            f"  profile: {profile}",
            f"  range: {data.get('start')} to {data.get('end')}",
            f"  count: {len(events)}",
        ]
        for e in events:
            start = (e.get("start") or "")[:16]
            lines.append(
                f"- [{e.get('id')}] {e.get('title')}  {start}  ({e.get('type')})"
            )
        if not events:
            lines.append("  (no events)")
        return "\n".join(lines)

    if command == "timetable":
        lessons = data.get("lessons", [])
        days = data.get("days", [])
        lines = [
            "Timetable",
            f"  profile: {profile}",
            f"  date: {data.get('start_date', 'this week')}",
            f"  days: {', '.join(d.get('header', '?') for d in days)}",
        ]
        by_day: dict[str, list[dict]] = {}
        for lesson in lessons:
            by_day.setdefault(lesson.get("day", ""), []).append(lesson)
        for day_name, day_lessons in by_day.items():
            marker = (
                " *"
                if any(d.get("is_today") and d.get("header") == day_name for d in days)
                else ""
            )
            lines.append(f"\n[{day_name}{marker}]")
            for l in day_lessons:
                p = l.get("period") or "?"
                t = l.get("time") or "?"
                s = l.get("subject") or "?"
                w = l.get("teacher") or "?"
                r = l.get("room") or ""
                lines.append(f"  {p:>12}  {t:>22}  {s:<30}  {w:<25}  {r}")
        if not lessons:
            lines.append("  (no lessons)")
        return "\n".join(lines)

    if command == "grades":
        tasks = data.get("tasks", [])
        categories = data.get("categories", [])
        expected = data.get("expected_grade", {}) or {}
        lines = [
            "Class grades",
            f"  profile: {profile}",
            f"  class_id: {data.get('class_id')}",
        ]
        if expected:
            lines.append(
                f"  expected_grade: {expected.get('letter_grade', '?')} "
                f"(avg {expected.get('average_score', '?')}, "
                f"n={expected.get('num_graded', '?')})"
            )
        if categories:
            lines.append("\n  [categories]")
            for c in categories:
                lines.append(f"  - {c.get('name')}: {c.get('weight', 0) * 100:.0f}%")
        if tasks:
            lines.append("\n  [tasks]")
            for t in tasks:
                grade = t.get("grade_letter") or t.get("status") or "-"
                pts = t.get("points") or ""
                cat = t.get("category") or ""
                lines.append(
                    f"- {t.get('task_id', '?'):>10}  {grade:<4}  "
                    f"{pts:<16}  {cat:<25}  {t.get('title', '?')}"
                )
        else:
            lines.append("  (no tasks)")
        return "\n".join(lines)

    if command == "grades.list":
        classes = data.get("classes", [])
        lines = ["Classes"]
        for c in classes:
            lines.append(f"  {c.get('id', '?'):<12}  {c.get('name', '?')}")
        if not classes:
            lines.append("  (none)")
        return "\n".join(lines)

    return json.dumps(payload, indent=2, ensure_ascii=False)


def print_payload(
    payload: dict, output_path: str | None = None, requested_format: str | None = None
) -> None:
    output_format = resolve_format(requested_format)
    rendered = (
        json.dumps(payload, indent=2, ensure_ascii=False)
        if output_format == "json"
        else render_pretty(payload)
    )
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
            f.write("\n")
    else:
        print(rendered)


def ok(command: str, profile: str, data: dict) -> dict:
    return {
        "ok": True,
        "command": command,
        "profile": profile,
        "data": data,
    }


def error(command: str, code: str, message: str) -> dict:
    return {
        "ok": False,
        "command": command,
        "error": {
            "code": code,
            "message": message,
        },
    }
