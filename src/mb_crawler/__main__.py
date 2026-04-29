"""CLI entry-point for ``mb-crawler`` / ``python -m mb_crawler``."""

from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import json
import sys
from textwrap import indent

import re
from datetime import date, timedelta

from .client import ManageBacClient
from .cache import ResponseCache
from .config import clear_session, load_state, save_profile, save_session
from .daemon import configure_webhook, load_daemon_config, start_loop, stop_daemon
from .notifications import MNNHubClient, hub_for_domain


def _resolve_format(requested_format: str | None) -> str:
    if requested_format:
        return requested_format
    return "pretty" if sys.stdout.isatty() else "json"


def _render_pretty(payload: dict) -> str:
    if not payload.get("ok"):
        error = payload.get("error", {})
        return f"ERROR [{error.get('code', 'unknown')}]: {error.get('message', 'Unknown error')}"

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
                    f"- {task.get('id')} | {task.get('title')} | {task.get('class_name')} | {task.get('due_date')} | {grade}"
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
                    f"- {attachment.get('source')}: {attachment.get('name')} -> {attachment.get('url')}"
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
        # Group lessons by day
        by_day: dict[str, list[dict]] = {}
        for l in lessons:
            by_day.setdefault(l.get("day", ""), []).append(l)
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
                f"(avg {expected.get('average_score', '?')}, n={expected.get('num_graded', '?')})"
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
                    f"- {t.get('task_id', '?'):>10}  {grade:<4}  {pts:<16}  {cat:<25}  {t.get('title', '?')}"
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


def _print_payload(
    payload: dict, output_path: str | None = None, requested_format: str | None = None
) -> None:
    output_format = _resolve_format(requested_format)
    rendered = (
        json.dumps(payload, indent=2, ensure_ascii=False)
        if output_format == "json"
        else _render_pretty(payload)
    )
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
            f.write("\n")
    else:
        print(rendered)


def _ok(command: str, profile: str, data: dict) -> dict:
    return {
        "ok": True,
        "command": command,
        "profile": profile,
        "data": data,
    }


def _error(command: str, code: str, message: str) -> dict:
    return {
        "ok": False,
        "command": command,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _matches_subject(task: dict, subject: str) -> bool:
    class_name = task.get("class_name")
    if not class_name:
        return False
    return subject.casefold() in class_name.casefold()


def _filter_result_by_subject(result: dict, subject: str) -> dict:
    upcoming = [task for task in result["upcoming"] if _matches_subject(task, subject)]
    past = [task for task in result["past"] if _matches_subject(task, subject)]
    overdue = [task for task in result["overdue"] if _matches_subject(task, subject)]

    result["upcoming"] = upcoming
    result["past"] = past
    result["overdue"] = overdue
    result["summary"] = {
        "upcoming_count": len(upcoming),
        "past_count": len(past),
        "overdue_count": len(overdue),
        "total_count": len(upcoming) + len(past) + len(overdue),
    }
    result["subject_filter"] = subject
    return result


def _build_client(args, command: str):
    state = load_state(args.profile, args.config, args.session_file)
    school = args.school or state.profile.school or state.session.school
    domain = (
        args.domain or state.profile.domain or state.session.domain or "managebac.com"
    )
    email = args.email or state.profile.email or state.session.email
    if not school:
        raise ValueError(
            json.dumps(
                _error(
                    command, "missing_credentials", "Missing school in args or config"
                )
            )
        )
    cache_ttl = getattr(args, "cache_ttl", None)
    if cache_ttl is None:
        cache_ttl = state.profile.default_cache_ttl
    cache_kwargs: dict = {"enabled": not getattr(args, "refresh", False)}
    if cache_ttl is not None:
        cache_kwargs["ttl"] = cache_ttl
    cache = ResponseCache(**cache_kwargs)
    client = ManageBacClient(school, domain=domain, cache=cache)
    return state, client, email


def _authenticate_client(args, state, client, email: str | None, command: str) -> str:
    if args.cookie:
        client.set_cookie(args.cookie)
        cookie_value = args.cookie
    elif state.session.cookie and not args.reauth:
        client.set_cookie(state.session.cookie)
        cookie_value = state.session.cookie
    else:
        email_value = email or args.email
        if not email_value:
            raise ValueError(
                json.dumps(
                    _error(
                        command,
                        "missing_credentials",
                        "Missing email in args or config",
                    )
                )
            )
        password = args.password or getpass.getpass("ManageBac password: ")
        if not client.login(email_value, password):
            raise ValueError(
                json.dumps(
                    _error(command, "authentication_failed", "ManageBac login failed")
                )
            )
        cookie_value = client.session.cookies.get("_managebac_session")
        email = email_value

    state.profile.school = client.school
    state.profile.domain = client.domain
    state.profile.email = email or state.profile.email
    save_profile(state)

    state.session.school = client.school
    state.session.domain = client.domain
    state.session.email = email or state.session.email
    state.session.base_url = client.base
    state.session.cookie = cookie_value
    state.session.logged_in_at = datetime.now().isoformat()
    save_session(state)
    return email or state.profile.email or ""


def _result_views(result: dict, requested_view: str) -> dict:
    if requested_view == "upcoming":
        return {"upcoming": result["upcoming"], "past": [], "overdue": []}
    if requested_view == "past":
        return {"upcoming": [], "past": result["past"], "overdue": []}
    if requested_view == "overdue":
        return {"upcoming": [], "past": [], "overdue": result["overdue"]}
    return {
        "upcoming": result["upcoming"],
        "past": result["past"],
        "overdue": result["overdue"],
    }


def cmd_login(args) -> int:
    state, client, email = _build_client(args, "login")
    email = _authenticate_client(args, state, client, email, "login")
    payload = _ok(
        "login",
        state.active_profile,
        {
            "school": client.school,
            "domain": client.domain,
            "email": email,
            "base_url": client.base,
            "auth_method": "cookie" if args.cookie else "password",
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_list(args) -> int:
    state, client, email = _build_client(args, "list")
    _authenticate_client(args, state, client, email, "list")

    pages = args.pages or state.profile.default_pages
    details = (
        args.details if args.details is not None else state.profile.default_details
    )
    view = args.view or state.profile.default_view
    subject = args.subject or state.profile.default_subject or None

    result = client.crawl_all(max_pages=pages, fetch_details=details)
    if subject:
        result = _filter_result_by_subject(result, subject)

    views = _result_views(result, view)
    summary = {
        "upcoming_count": len(views["upcoming"]),
        "past_count": len(views["past"]),
        "overdue_count": len(views["overdue"]),
        "total_count": len(views["upcoming"])
        + len(views["past"])
        + len(views["overdue"]),
    }
    payload = _ok(
        "list",
        state.active_profile,
        {
            "meta": {
                "student_name": result["student_name"],
                "school": result["school"],
                "domain": client.domain,
                "base_url": result["base_url"],
                "crawled_at": result["crawled_at"],
                "view": view,
                "subject_filter": subject,
                "details": details,
            },
            "summary": summary,
            "tasks": views,
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def _find_task_by_id(result: dict, task_id: str) -> dict | None:
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            return task
    return None


def cmd_view(args) -> int:
    state, client, email = _build_client(args, "view")
    _authenticate_client(args, state, client, email, "view")

    target = args.target or args.id or args.url
    task = None
    detail = None

    if target and (
        target.startswith("http://")
        or target.startswith("https://")
        or "/core_tasks/" in target
    ):
        detail = client.get_task_detail(target)
        task = {"id": target.split("core_tasks/")[-1].split("/")[0], "link": target}
    else:
        task_id = args.id or args.target
        if not task_id:
            payload = _error("view", "missing_target", "Provide a task id or task url")
            _print_payload(payload, args.output, args.format)
            return 1
        result = client.crawl_all(max_pages=args.pages, fetch_details=False)
        if args.subject:
            result = _filter_result_by_subject(result, args.subject)
        task = _find_task_by_id(result, task_id)
        if not task:
            payload = _error(
                "view", "task_not_found", f"No task found for id {task_id}"
            )
            _print_payload(payload, args.output, args.format)
            return 1
        detail = client.get_task_detail(task["link"])

    payload = _ok(
        "view",
        state.active_profile,
        {
            "task": task,
            "detail": detail,
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_logout(args) -> int:
    state = load_state(args.profile, args.config, args.session_file)
    clear_session(state, all_profiles=args.all)
    payload = _ok(
        "logout",
        state.active_profile,
        {
            "logged_out": True,
            "all_profiles": args.all,
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_start(args) -> int:
    state, client, email = _build_client(args, "daemon")
    _authenticate_client(args, state, client, email, "daemon")
    daemon_config = load_daemon_config(args.daemon_config)
    if args.webhook_url:
        daemon_config["webhook_url"] = args.webhook_url
    if args.interval is not None:
        daemon_config["interval"] = args.interval
    result = start_loop(client, daemon_config, dry_run=args.dry_run, once=args.once)
    payload = _ok(
        "daemon.start", state.active_profile, result | {"daemon": daemon_config}
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_stop(args) -> int:
    result = stop_daemon(args.daemon_config)
    payload = _ok("daemon.stop", "default", result)
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_configure_webhook(args) -> int:
    config = configure_webhook(args.url, args.daemon_config)
    payload = _ok("daemon.configure-webhook", "default", config)
    _print_payload(payload, args.output, args.format)
    return 0


def _resolve_task_ids(
    client: ManageBacClient, target: str, pages: int = 10
) -> tuple[str, str]:
    """Resolve a task target (id, URL, or class/task pair) to (class_id, task_id)."""
    if target.startswith("http") or "/core_tasks/" in target:
        m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", target)
        if m:
            return m.group(1), m.group(2)
        # Try resolving by id from crawl
        parts = target.rstrip("/").split("/")
        task_id = parts[-1]
    else:
        task_id = target

    result = client.crawl_all(max_pages=pages, fetch_details=False)
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            m = re.search(
                r"/student/classes/(\d+)/core_tasks/(\d+)", task.get("link", "")
            )
            if m:
                return m.group(1), m.group(2)
    raise ValueError(f"Could not find task with id {task_id}")


def cmd_submit(args) -> int:
    state, client, email = _build_client(args, "submit")
    _authenticate_client(args, state, client, email, "submit")

    target = args.target
    if not target:
        payload = _error(
            "submit", "missing_target", "Provide a task id or URL and file path"
        )
        _print_payload(payload, args.output, args.format)
        return 1

    file_path = args.file
    if not file_path:
        payload = _error("submit", "missing_file", "Provide a file path to upload")
        _print_payload(payload, args.output, args.format)
        return 1

    try:
        class_id, task_id = _resolve_task_ids(client, target, args.pages)
    except ValueError as e:
        payload = _error("submit", "task_not_found", str(e))
        _print_payload(payload, args.output, args.format)
        return 1

    try:
        result = client.submit_file(class_id, task_id, file_path)
    except (FileNotFoundError, RuntimeError) as e:
        payload = _error("submit", "upload_failed", str(e))
        _print_payload(payload, args.output, args.format)
        return 1

    payload = _ok("submit", state.active_profile, result)
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_notifications(args) -> int:
    state, client, email = _build_client(args, "notifications")
    _authenticate_client(args, state, client, email, "notifications")

    hub_endpoint, token = client.get_notification_token()
    from .notifications import hub_for_domain

    if not hub_endpoint:
        hub_endpoint = hub_for_domain(client.domain)
    hub = MNNHubClient(hub_endpoint, token)

    # Action: mark as read
    if args.read is not None:
        ok = hub.mark_read(args.read)
        payload = _ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "read",
                "notification_id": args.read,
                "ok": ok,
            },
        )
        _print_payload(payload, args.output, args.format)
        return 0

    if args.unread is not None:
        ok = hub.mark_unread(args.unread)
        payload = _ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "unread",
                "notification_id": args.unread,
                "ok": ok,
            },
        )
        _print_payload(payload, args.output, args.format)
        return 0

    if args.read_all:
        ok = hub.mark_all_read()
        payload = _ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "read_all",
                "notification_id": None,
                "ok": ok,
            },
        )
        _print_payload(payload, args.output, args.format)
        return 0

    # Default: list notifications
    stats = hub.stats()
    result = hub.list(page=args.page, per_page=args.per_page)
    payload = _ok(
        "notifications",
        state.active_profile,
        {
            "stats": stats,
            "items": result["items"],
            "meta": result["meta"],
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_calendar(args) -> int:
    state, client, email = _build_client(args, "calendar")
    _authenticate_client(args, state, client, email, "calendar")

    today = date.today()

    if args.ical:
        ical_text = client.get_ical_feed()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(ical_text)
        else:
            print(ical_text)
        return 0

    if args.today:
        start = today.isoformat()
        end = today.isoformat()
    elif args.start and args.end:
        start = args.start
        end = args.end
    elif args.start:
        start = args.start
        d = date.fromisoformat(start)
        end = (d + timedelta(days=6)).isoformat()
    else:
        start = today.isoformat()
        end = (today + timedelta(days=6)).isoformat()

    events = client.get_calendar_events(start, end)
    payload = _ok(
        "calendar",
        state.active_profile,
        {
            "start": start,
            "end": end,
            "events": events,
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_timetable(args) -> int:
    state, client, email = _build_client(args, "timetable")
    _authenticate_client(args, state, client, email, "timetable")

    start_date = args.date
    if args.today:
        start_date = date.today().isoformat()

    result = client.get_timetable(start_date)
    payload = _ok(
        "timetable",
        state.active_profile,
        {
            "start_date": start_date or "this week",
            "days": result["days"],
            "lessons": result["lessons"],
        },
    )
    _print_payload(payload, args.output, args.format)
    return 0


def cmd_grades(args) -> int:
    state, client, email = _build_client(args, "grades")
    _authenticate_client(args, state, client, email, "grades")

    class_id = args.class_id
    if not class_id:
        # Resolve by fuzzy name match
        result = client.crawl_all(max_pages=5, fetch_details=False)
        seen: dict[str, str] = {}
        for task in result["upcoming"] + result["past"] + result["overdue"]:
            link = task.get("link", "")
            m = re.search(r"/student/classes/(\d+)/", link)
            cname = task.get("class_name", "")
            if m and cname:
                seen[m.group(1)] = cname
        if not seen:
            payload = _error("grades", "no_classes", "No classes found")
            _print_payload(payload, args.output, args.format)
            return 1
        if args.subject:
            # Fuzzy match
            for cid, cname in seen.items():
                if args.subject.lower() in cname.lower():
                    class_id = cid
                    break
            if not class_id:
                payload = _error(
                    "grades", "class_not_found", f"No class matching '{args.subject}'"
                )
                _print_payload(payload, args.output, args.format)
                return 1
        else:
            # List all classes found
            payload = _ok(
                "grades.list",
                state.active_profile,
                {
                    "classes": [
                        {"id": cid, "name": cname} for cid, cname in seen.items()
                    ],
                },
            )
            _print_payload(payload, args.output, args.format)
            return 0

    grades = client.get_class_grades(class_id)
    grades["class_id"] = class_id
    payload = _ok("grades", state.active_profile, grades)
    _print_payload(payload, args.output, args.format)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mb-crawler",
        description="Crawl ManageBac tasks, grades & submissions",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_auth_flags(subparser, include_password: bool = True):
        subparser.add_argument(
            "--profile",
            default=None,
            help="Profile name (default: active_profile or default)",
        )
        subparser.add_argument("--config", help="Path to config TOML")
        subparser.add_argument("--session-file", help="Path to session TOML")
        subparser.add_argument("--school", help="School subdomain (e.g. bj80)")
        subparser.add_argument("--domain", "-d", help="Base domain (e.g. managebac.cn)")
        subparser.add_argument("--email", "-e", help="Login email")
        if include_password:
            subparser.add_argument("--password", "-p", help="Login password")
        subparser.add_argument(
            "--cookie", "-c", help="Session cookie (_managebac_session)"
        )
        subparser.add_argument(
            "--reauth",
            action="store_true",
            help="Force re-login instead of reusing saved session",
        )
        subparser.add_argument(
            "--refresh",
            action="store_true",
            help="Bypass response cache and fetch fresh data",
        )
        subparser.add_argument(
            "--cache-ttl",
            type=int,
            default=None,
            help="Cache TTL in seconds (default: 1800, i.e. 30 min)",
        )
        subparser.add_argument("--output", "-o", help="Write output to file")
        subparser.add_argument(
            "--format",
            choices=["pretty", "json"],
            default=None,
            help="Output format (default: pretty for TTY, json otherwise)",
        )

    login = subparsers.add_parser("login", help="Authenticate and persist session")
    add_common_auth_flags(login)
    login.set_defaults(func=cmd_login)

    list_parser = subparsers.add_parser("list", help="List ManageBac tasks")
    add_common_auth_flags(list_parser)
    list_parser.add_argument(
        "--subject", "-s", help="Filter tasks by subject/class name"
    )
    list_parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Max pages per view (default: from config, 10)",
    )
    list_parser.add_argument(
        "--details", action="store_true", default=None, help="Fetch task detail pages"
    )
    list_parser.add_argument(
        "--view",
        choices=["all", "upcoming", "past", "overdue"],
        default=None,
        help="Restrict output to one view or all views (default: from config, all)",
    )
    list_parser.set_defaults(func=cmd_list)

    view = subparsers.add_parser("view", help="View one task in detail")
    add_common_auth_flags(view)
    view.add_argument("target", nargs="?", help="Task id or task URL")
    view.add_argument("--id", help="Task id")
    view.add_argument("--url", help="Task URL")
    view.add_argument("--subject", help="Optional subject filter when resolving by id")
    view.add_argument(
        "--pages", type=int, default=10, help="Max pages to search when resolving by id"
    )
    view.set_defaults(func=cmd_view)

    logout = subparsers.add_parser("logout", help="Clear persisted session")
    logout.add_argument("--profile", default=None, help="Profile name")
    logout.add_argument("--config", help="Path to config TOML")
    logout.add_argument("--session-file", help="Path to session TOML")
    logout.add_argument("--all", action="store_true", help="Remove all saved sessions")
    logout.add_argument("--output", "-o", help="Write output to file")
    logout.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon = subparsers.add_parser("daemon", help="Manage webhook daemon")
    daemon_subparsers = daemon.add_subparsers(dest="daemon_command", required=True)

    daemon_start = daemon_subparsers.add_parser(
        "start", help="Start daemon loop or run one cycle"
    )
    add_common_auth_flags(daemon_start)
    daemon_start.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_start.add_argument("--webhook-url", help="Override webhook URL for this run")
    daemon_start.add_argument(
        "--interval", type=int, help="Polling interval in seconds"
    )
    daemon_start.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not POST webhook, only compute alerts",
    )
    daemon_start.add_argument(
        "--once", action="store_true", help="Run one cycle and exit"
    )
    daemon_start.set_defaults(func=cmd_daemon_start)

    daemon_stop = daemon_subparsers.add_parser("stop", help="Stop daemon loop")
    daemon_stop.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_stop.add_argument("--output", "-o", help="Write output to file")
    daemon_stop.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_stop.set_defaults(func=cmd_daemon_stop)

    daemon_configure = daemon_subparsers.add_parser(
        "configure-webhook", help="Persist daemon webhook URL"
    )
    daemon_configure.add_argument("url", help="Webhook URL")
    daemon_configure.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_configure.add_argument("--output", "-o", help="Write output to file")
    daemon_configure.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_configure.set_defaults(func=cmd_daemon_configure_webhook)

    # ── submit ──────────────────────────────────────────────────────────
    submit = subparsers.add_parser("submit", help="Upload a file to a task dropbox")
    add_common_auth_flags(submit)
    submit.add_argument("target", nargs="?", help="Task id or URL")
    submit.add_argument("file", nargs="?", help="File path to upload")
    submit.add_argument("--id", help="Task id")
    submit.add_argument(
        "--pages", type=int, default=10, help="Max pages to search when resolving by id"
    )
    submit.set_defaults(func=cmd_submit)

    # ── notifications ───────────────────────────────────────────────────
    notifications = subparsers.add_parser(
        "notifications", help="View and manage notifications"
    )
    add_common_auth_flags(notifications)
    notifications.add_argument(
        "--page", type=int, default=1, help="Page number (default: 1)"
    )
    notifications.add_argument(
        "--per-page", type=int, default=20, help="Items per page (default: 20)"
    )
    notifications.add_argument(
        "--read", type=int, metavar="ID", help="Mark notification as read"
    )
    notifications.add_argument(
        "--unread", type=int, metavar="ID", help="Mark notification as unread"
    )
    notifications.add_argument(
        "--read-all", action="store_true", help="Mark all notifications as read"
    )
    notifications.set_defaults(func=cmd_notifications)

    # ── calendar ────────────────────────────────────────────────────────
    calendar_p = subparsers.add_parser("calendar", help="View calendar events")
    add_common_auth_flags(calendar_p)
    calendar_p.add_argument("--start", help="Start date (YYYY-MM-DD)")
    calendar_p.add_argument("--end", help="End date (YYYY-MM-DD)")
    calendar_p.add_argument("--today", action="store_true", help="Show today only")
    calendar_p.add_argument("--ical", action="store_true", help="Output raw iCal feed")
    calendar_p.set_defaults(func=cmd_calendar)

    # ── timetable ───────────────────────────────────────────────────────
    timetable_p = subparsers.add_parser("timetable", help="View weekly timetable")
    add_common_auth_flags(timetable_p)
    timetable_p.add_argument("--date", help="Start date of week (YYYY-MM-DD)")
    timetable_p.add_argument("--today", action="store_true", help="Show this week")
    timetable_p.set_defaults(func=cmd_timetable)

    # ── grades ──────────────────────────────────────────────────────────
    grades_p = subparsers.add_parser(
        "grades", help="View class grades and expected grade"
    )
    add_common_auth_flags(grades_p)
    grades_p.add_argument("--class-id", help="Class ID (numeric)")
    grades_p.add_argument("--subject", "-s", help="Fuzzy match class name")
    grades_p.set_defaults(func=cmd_grades)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except ValueError as exc:
        try:
            payload = json.loads(str(exc))
        except json.JSONDecodeError:
            payload = _error(args.command, "unexpected_error", str(exc))
        _print_payload(payload, args.output, getattr(args, "format", None))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
