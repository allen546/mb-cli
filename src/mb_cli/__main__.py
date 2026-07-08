"""CLI entry-point for ``mb`` / ``python -m mb_cli``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .auth import build_client
from .client import ManageBacClient
from .config import clear_session, load_state, save_profile, save_session
from .daemon import (
    configure_channel_send,
    configure_webhook,
    load_daemon_config,
    start_loop,
    stop_daemon,
)
from .exceptions import CommandError
from .filters import (
    filter_result_by_subject,
    find_task_by_id,
    result_views,
)
from .formatters import error, ok, print_payload
from .notifications import MNNHubClient, hub_for_domain

log = logging.getLogger(__name__)


# ── Client helpers ──────────────────────────────────────────────────────


def _build_client(args, command: str):
    """CLI wrapper: maps argparse namespace to :func:`auth.build_client`."""
    password = getattr(args, "password", None)
    if not password and not args.cookie:
        state = load_state(args.profile, args.config, args.session_file)
        if not state.session.cookie or getattr(args, "reauth", False):
            password = getpass.getpass("ManageBac password: ")
    verify = not getattr(args, "no_verify_tls", False)
    return build_client(
        school=args.school,
        domain=args.domain,
        email=args.email,
        password=password,
        cookie=args.cookie,
        profile=args.profile,
        refresh=getattr(args, "refresh", False),
        reauth=getattr(args, "reauth", False),
        verify=verify,
        cache_ttl=getattr(args, "cache_ttl", None),
        retry=getattr(args, "retry", 3),
        remember=not getattr(args, "temp", False),
    )


def _authenticate_client(state, client, email: str) -> str:
    """Persist auth state to disk (CLI-specific)."""
    state.profile.school = client.school
    state.profile.domain = client.domain
    state.profile.email = email or state.profile.email
    save_profile(state)

    state.session.school = client.school
    state.session.domain = client.domain
    state.session.email = email or state.session.email
    state.session.base_url = client.base
    state.session.cookie = client.session.cookies.get("_managebac_session")
    state.session.logged_in_at = datetime.now().isoformat()
    save_session(state)
    return email or state.profile.email or ""


DEFAULT_SNAPSHOT_PATH = Path.home() / ".config" / "mb-crawler" / "snapshot.json"


def load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"upcoming": [], "past": [], "overdue": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"upcoming": [], "past": [], "overdue": []}


def save_snapshot(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save snapshot: %s", e)


def merge_snapshot(old: dict, new: dict, client=None) -> dict:
    """Merge new crawl results into the old snapshot.

    1. Tasks present in new crawl overwrite those in the old snapshot.
    2. Tasks present in old snapshot but missing in the new crawl are preserved,
       and marked with "deleted_from_server": True.
    3. If client is provided, invalidate cache for task details if grade or status changes.
    """
    merged_map = {}

    # Determine reference datetime for date classifications
    now_ref = datetime.now()
    crawled_at_str = new.get("crawled_at") or old.get("crawled_at")
    if crawled_at_str:
        try:
            now_ref = datetime.fromisoformat(crawled_at_str)
        except Exception:
            pass

    # helper to build map from snapshot sections
    for section in ("upcoming", "past", "overdue"):
        for t in old.get(section, []):
            tid = t.get("id")
            if tid:
                merged_map[tid] = t

    # Update with new results
    new_tids = set()
    for section in ("upcoming", "past", "overdue"):
        for t in new.get(section, []):
            tid = t.get("id")
            if tid:
                new_tids.add(tid)
                old_t = merged_map.get(tid)
                if old_t:
                    # Opportunistic cache invalidation
                    # Check if grade or status/labels changed
                    grade_changed = old_t.get("grade_letter") != t.get("grade_letter") or old_t.get("grade_score") != t.get("grade_score")
                    old_labels = old_t.get("labels") or []
                    new_labels = t.get("labels") or []
                    labels_changed = set(old_labels) != set(new_labels) or old_t.get("status") != t.get("status")

                    if (grade_changed or labels_changed) and client:
                        class_link = t.get("link") or ""
                        m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", class_link)
                        if m:
                            cid, task_id = m.group(1), m.group(2)
                            detail_url = f"{client.base}/student/classes/{cid}/core_tasks/{task_id}"
                            hint_url = f"{client.base}/student/classes/{cid}/events/{task_id}/hint"
                            dropbox_url = f"{client.base}/student/classes/{cid}/core_tasks/{task_id}/dropbox"
                            client.cache.invalidate(detail_url)
                            client.cache.invalidate(hint_url)
                            client.cache.invalidate(dropbox_url)
                            log.info("Task %s state changed; invalidated cached details.", task_id)
                merged_map[tid] = t

    # Mark tasks in snapshot that were NOT in the new crawl as deleted from server
    for tid, t in merged_map.items():
        if tid not in new_tids:
            t["deleted_from_server"] = True

    # Reclassify all merged tasks into upcoming, past, overdue based on due_date and status
    upcoming = []
    past = []
    overdue = []

    from .client import parse_due_date

    for t in merged_map.values():
        due_date = t.get("due_date")
        due_dt = parse_due_date(due_date, now_ref=now_ref)

        labels = t.get("labels") or []
        status = t.get("status")
        labels_lower = [l.lower() for l in labels]
        is_submitted = False
        if "submitted" in labels_lower or status == "submitted":
            is_submitted = True

        grade_letter = t.get("grade_letter")
        grade_score = t.get("grade_score")

        is_zero_score = False
        if grade_score:
            if re.match(r"^\s*0\s*/", grade_score):
                is_zero_score = True

        has_score = bool(grade_score and grade_score.strip() and grade_score.strip() != "-")
        has_letter = bool(grade_letter and grade_letter.strip())
        has_completed_grade = (has_score or has_letter) and not is_zero_score

        is_not_assessed = "not assessed yet" in labels_lower or (bool(grade_letter) and "not assessed" in grade_letter.lower())
        has_submit_btn = bool(t.get("has_submit_button", False))
        t["has_submit_button"] = has_submit_btn
        is_unfinished = has_submit_btn and (not is_submitted) and (not has_completed_grade) and (not is_not_assessed)

        if due_dt and due_dt > now_ref:
            t["view"] = "upcoming"
            upcoming.append(t)
        elif is_unfinished:
            t["view"] = "overdue"
            overdue.append(t)
        else:
            t["view"] = "past"
            past.append(t)

    return {
        "student_name": new.get("student_name") or old.get("student_name"),
        "school": new.get("school") or old.get("school"),
        "base_url": new.get("base_url") or old.get("base_url"),
        "crawled_at": new.get("crawled_at") or old.get("crawled_at"),
        "upcoming": upcoming,
        "past": past,
        "overdue": overdue,
    }


# ── Commands ────────────────────────────────────────────────────────────


def cmd_login(args) -> int:
    state, client, email = _build_client(args, "login")
    email = _authenticate_client(state, client, email)
    payload = ok(
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
    print_payload(payload, args.output, args.format)
    return 0


def cmd_list(args) -> int:
    state, client, email = _build_client(args, "list")
    _authenticate_client(state, client, email)

    pages = args.pages or state.profile.default_pages
    details = (
        args.details if args.details is not None else state.profile.default_details
    )
    view = args.view or state.profile.default_view
    subject = args.subject or state.profile.default_subject or None

    from .filters import filter_result_by_subject, filter_result_by_status

    # Load local snapshot
    snapshot_path = state.config_path.parent / "snapshot.json"
    old_snapshot = load_snapshot(snapshot_path)

    # Check if we can reuse the snapshot (crawled within last 15 minutes)
    use_cached_snapshot = False
    if old_snapshot and not args.refresh:
        crawled_at_str = old_snapshot.get("crawled_at")
        if crawled_at_str:
            try:
                from datetime import datetime
                crawled_at = datetime.fromisoformat(crawled_at_str)
                age = (datetime.now() - crawled_at).total_seconds()
                if age < 900:  # 15 minutes TTL
                    use_cached_snapshot = True
                    log.info("Using cached snapshot (age: %d seconds)", int(age))
            except Exception:
                pass

    if use_cached_snapshot:
        merged_result = old_snapshot
    else:
        # Fetch fresh results
        new_result = client.crawl_all(max_pages=pages, fetch_details=details)
        # Merge with local snapshot and save
        merged_result = merge_snapshot(old_snapshot, new_result, client=client)
        save_snapshot(snapshot_path, merged_result)

    # Filter out tasks that were deleted from the server (unless --deleted is specified)
    show_deleted = getattr(args, "deleted", False)
    result = {
        "student_name": merged_result.get("student_name"),
        "school": merged_result.get("school"),
        "base_url": merged_result.get("base_url"),
        "crawled_at": merged_result.get("crawled_at"),
        "upcoming": [t for t in merged_result.get("upcoming", []) if show_deleted or not t.get("deleted_from_server")],
        "past": [t for t in merged_result.get("past", []) if show_deleted or not t.get("deleted_from_server")],
        "overdue": [t for t in merged_result.get("overdue", []) if show_deleted or not t.get("deleted_from_server")]
    }

    if subject:
        result = filter_result_by_subject(result, subject)

    # Apply status and tag filters (graded, submitted, grade, tag, completed)
    completed_val = None
    if args.completed:
        completed_val = True
    elif args.todo:
        completed_val = False

    if (
        args.graded is not None
        or args.submitted is not None
        or args.grade is not None
        or args.tag is not None
        or completed_val is not None
    ):
        result = filter_result_by_status(
            result,
            graded=args.graded,
            submitted=args.submitted,
            grade=args.grade,
            tag=args.tag,
            completed=completed_val,
        )

    views = result_views(result, view)
    summary = {
        "upcoming_count": len(views["upcoming"]),
        "past_count": len(views["past"]),
        "overdue_count": len(views["overdue"]),
        "total_count": len(views["upcoming"])
        + len(views["past"])
        + len(views["overdue"]),
    }
    payload = ok(
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
                "graded_filter": args.graded,
                "submitted_filter": args.submitted,
                "grade_filter": args.grade,
                "tag_filter": args.tag,
                "todo_filter": args.todo,
                "completed_filter": args.completed,
                "details": details,
            },
            "summary": summary,
            "tasks": views,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_view(args) -> int:
    state, client, email = _build_client(args, "view")
    _authenticate_client(state, client, email)

    target = args.target or args.id or args.url
    task = None
    detail = None

    if target and (
        target.startswith("http://")
        or target.startswith("https://")
        or "/core_tasks/" in target
    ):
        task_id = target.split("core_tasks/")[-1].split("/")[0]
        # Search local snapshot first to populate standard fields
        snapshot_path = state.config_path.parent / "snapshot.json"
        snapshot = load_snapshot(snapshot_path)
        task = find_task_by_id(snapshot, task_id)

        detail = client.get_task_detail(target, bypass_cache=args.refresh)
        if not task:
            task = {"id": task_id, "link": target}
    else:
        task_id = args.id or args.target
        if not task_id:
            payload = error("view", "missing_target", "Provide a task id or task url")
            print_payload(payload, args.output, args.format)
            return 1

        # 1. Search local snapshot first
        snapshot_path = state.config_path.parent / "snapshot.json"
        snapshot = load_snapshot(snapshot_path)
        task = find_task_by_id(snapshot, task_id)

        # 2. Fall back to sequential web crawl scan if not found
        if not task:
            log.info("Task %s not found in local snapshot. Performing sequential web crawl fallback...", task_id)
            fallback_task = client.find_task_by_id(task_id, max_pages=args.pages or 20)
            if isinstance(fallback_task, dict):
                task = fallback_task

        if not task:
            payload = error("view", "task_not_found", f"No task found for id {task_id}")
            print_payload(payload, args.output, args.format)
            return 1
        detail = client.get_task_detail(task["link"], from_hint=False, bypass_cache=args.refresh)

    # Merge parsed card details from detail page back into task metadata
    if detail and isinstance(detail, dict):
        for k, dest_key in (
            ("grade_letter", "grade_letter"),
            ("grade_score", "grade_score"),
            ("status", "status"),
            ("labels", "labels"),
            ("has_submit_button", "has_submit_button")
        ):
            if k in detail and task.get(dest_key) is None:
                task[dest_key] = detail[k]

    payload = ok(
        "view",
        state.active_profile,
        {
            "task": task,
            "detail": detail,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_logout(args) -> int:
    state = load_state(args.profile, args.config, args.session_file)
    clear_session(state, all_profiles=args.all)
    payload = ok(
        "logout",
        state.active_profile,
        {
            "logged_out": True,
            "all_profiles": args.all,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_start(args) -> int:
    state, client, email = _build_client(args, "daemon")
    _authenticate_client(state, client, email)
    daemon_config = load_daemon_config(args.daemon_config)
    if args.webhook_url:
        daemon_config["delivery"] = {
            "mode": "webhook",
            "webhook_url": args.webhook_url,
        }
    if args.channel_id and args.recipient:
        daemon_config["delivery"] = {
            "mode": "channel_send",
            "channel_id": args.channel_id,
            "recipient": args.recipient,
        }
    if args.interval is not None:
        daemon_config["interval"] = args.interval
    if args.active_hours_start is not None:
        daemon_config["active_hours_start"] = args.active_hours_start
    if args.active_hours_end is not None:
        daemon_config["active_hours_end"] = args.active_hours_end
    result = start_loop(client, daemon_config, dry_run=args.dry_run, once=args.once)
    payload = ok(
        "daemon.start", state.active_profile, result | {"daemon": daemon_config}
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_stop(args) -> int:
    result = stop_daemon(args.daemon_config)
    payload = ok("daemon.stop", "default", result)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_configure_webhook(args) -> int:
    config = configure_webhook(args.url, args.daemon_config)
    payload = ok("daemon.configure-webhook", "default", config)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_configure_channel(args) -> int:
    config = configure_channel_send(args.channel_id, args.recipient, args.daemon_config)
    payload = ok("daemon.configure-channel", "default", config)
    print_payload(payload, args.output, args.format)
    return 0


def _resolve_task_ids(
    client: ManageBacClient, target: str, pages: int = 10
) -> tuple[str, str]:
    """Resolve a task target (id, URL, or class/task pair) to (class_id, task_id)."""
    if target.startswith("http") or "/core_tasks/" in target:
        m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", target)
        if m:
            return m.group(1), m.group(2)
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
    raise CommandError("task_not_found", f"Could not find task with id {task_id}")


def cmd_submit(args) -> int:
    state, client, email = _build_client(args, "submit")
    _authenticate_client(state, client, email)

    target = args.target
    if not target:
        payload = error(
            "submit", "missing_target", "Provide a task id or URL and file path"
        )
        print_payload(payload, args.output, args.format)
        return 1

    file_path = args.file
    if not file_path:
        payload = error("submit", "missing_file", "Provide a file path to upload")
        print_payload(payload, args.output, args.format)
        return 1

    try:
        class_id, task_id = _resolve_task_ids(client, target, args.pages)
    except CommandError as exc:
        payload = error("submit", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    try:
        result = client.submit_file(class_id, task_id, file_path)
    except (FileNotFoundError, RuntimeError) as exc:
        payload = error("submit", "upload_failed", str(exc))
        print_payload(payload, args.output, args.format)
        return 1

    payload = ok("submit", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_notifications(args) -> int:
    state, client, email = _build_client(args, "notifications")
    _authenticate_client(state, client, email)

    hub_endpoint, token = client.get_notification_token()
    if not hub_endpoint:
        hub_endpoint = hub_for_domain(client.domain)
    hub = MNNHubClient(hub_endpoint, token)

    if args.read is not None:
        ok_ = hub.mark_read(args.read)
        payload = ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "read",
                "notification_id": args.read,
                "ok": ok_,
            },
        )
        print_payload(payload, args.output, args.format)
        return 0

    if args.unread is not None:
        ok_ = hub.mark_unread(args.unread)
        payload = ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "unread",
                "notification_id": args.unread,
                "ok": ok_,
            },
        )
        print_payload(payload, args.output, args.format)
        return 0

    if args.read_all:
        ok_ = hub.mark_all_read()
        payload = ok(
            "notifications.mutate",
            state.active_profile,
            {
                "action": "read_all",
                "notification_id": None,
                "ok": ok_,
            },
        )
        print_payload(payload, args.output, args.format)
        return 0

    stats = hub.stats()
    result = hub.list(page=args.page, per_page=args.per_page)
    payload = ok(
        "notifications",
        state.active_profile,
        {
            "stats": stats,
            "items": result["items"],
            "meta": result["meta"],
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_calendar(args) -> int:
    state, client, email = _build_client(args, "calendar")
    _authenticate_client(state, client, email)

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
    payload = ok(
        "calendar",
        state.active_profile,
        {
            "start": start,
            "end": end,
            "events": events,
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_timetable(args) -> int:
    state, client, email = _build_client(args, "timetable")
    _authenticate_client(state, client, email)

    start_date = args.date
    if args.today:
        start_date = date.today().isoformat()

    result = client.get_timetable(start_date)
    payload = ok(
        "timetable",
        state.active_profile,
        {
            "start_date": start_date or "this week",
            "days": result["days"],
            "lessons": result["lessons"],
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_grades(args) -> int:
    state, client, email = _build_client(args, "grades")
    _authenticate_client(state, client, email)

    class_id = args.class_id
    if not class_id:
        result = client.crawl_all(max_pages=5, fetch_details=False)
        seen: dict[str, str] = {}
        for task in result["upcoming"] + result["past"] + result["overdue"]:
            link = task.get("link", "")
            m = re.search(r"/student/classes/(\d+)/", link)
            cname = task.get("class_name", "")
            if m and cname:
                seen[m.group(1)] = cname
        if not seen:
            payload = error("grades", "no_classes", "No classes found")
            print_payload(payload, args.output, args.format)
            return 1
        if args.subject:
            for cid, cname in seen.items():
                if args.subject.lower() in cname.lower():
                    class_id = cid
                    break
            if not class_id:
                payload = error(
                    "grades",
                    "class_not_found",
                    f"No class matching '{args.subject}'",
                )
                print_payload(payload, args.output, args.format)
                return 1
        else:
            # Gather grades for ALL classes
            all_grades = {}
            for cid, cname in seen.items():
                try:
                    c_grades = client.get_class_grades(cid)
                    c_grades["class_name"] = cname
                    all_grades[cid] = c_grades
                except Exception as e:
                    log.warning("failed to fetch grades for class %s: %s", cid, e)
            payload = ok(
                "grades.all",
                state.active_profile,
                {
                    "classes_grades": all_grades,
                },
            )
            print_payload(payload, args.output, args.format)
            return 0

    grades = client.get_class_grades(class_id)
    grades["class_id"] = class_id
    payload = ok("grades", state.active_profile, grades)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_count_grade_freq(args) -> int:
    state, client, email = _build_client(args, "count-grade-freq")
    _authenticate_client(state, client, email)

    result = client.count_grade_frequencies(class_filter=args.subject)
    if "error" in result:
        payload = error("count-grade-freq", "class_not_found", result["error"])
        print_payload(payload, args.output, args.format)
        return 1

    payload = ok("count-grade-freq", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9_\-]+", "_", text)
    return text.strip("_")


def cmd_download(args) -> int:
    state, client, email = _build_client(args, "download")
    _authenticate_client(state, client, email)

    task_id = args.task_id

    # 1. Look up task in snapshot first
    snapshot_path = state.config_path.parent / "snapshot.json"
    snapshot = load_snapshot(snapshot_path)

    task = find_task_by_id(snapshot, task_id)

    if not task:
        log.info("Task %s not found in local snapshot. Searching server...", task_id)
        task = client.find_task_by_id(task_id)
        if not task:
            log.error("Task %s not found on ManageBac.", task_id)
            return 1

    link = task.get("link")
    if not link:
        log.error("Task %s has no detail link.", task_id)
        return 1

    # 2. Fetch task details
    log.info("Fetching details for task %s...", task_id)
    detail = client.get_task_detail(link, from_hint=False)
    if not detail:
        log.error("Failed to fetch details for task %s.", task_id)
        return 1

    # 3. Determine output directory
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        title = task.get("title") or "task"
        slug = slugify(title)
        out_dir = Path(f"task_{task_id}_{slug}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 4. Collect files to download
    files_to_download = []
    attachments = detail.get("attachments", [])

    for att in attachments:
        source = att.get("source")
        name = att.get("name")
        url = att.get("url")
        if not name or not url:
            continue

        if source == "submission":
            if not args.no_submissions:
                files_to_download.append((name, url, "submission"))
        else:
            if not args.no_attachments:
                files_to_download.append((name, url, "attachment"))

    if not files_to_download:
        log.info("No matching attachments or submissions found to download.")
        return 0

    log.info("Downloading %d file(s) to %s...", len(files_to_download), out_dir)
    success_count = 0
    for name, url, source_type in files_to_download:
        dest_path = out_dir / name
        stem = Path(name).stem
        suffix = Path(name).suffix
        counter = 1
        while dest_path.exists():
            dest_path = out_dir / f"{stem} ({counter}){suffix}"
            counter += 1

        log.info("  [%s] Downloading %s...", source_type, name)
        try:
            with client.session.get(url, stream=True) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            log.info("    Saved as %s", dest_path.name)
            success_count += 1
        except Exception as e:
            log.error("    Failed to download %s: %s", name, e)

    log.info("Successfully downloaded %d/%d file(s).", success_count, len(files_to_download))
    return 0 if success_count > 0 else 1


# ── CLI parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mb",
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
            help="Cache TTL in seconds (default: 900, i.e. 15 min)",
        )
        subparser.add_argument(
            "--no-verify-tls",
            action="store_true",
            help="Disable TLS certificate verification (for self-hosted instances)",
        )
        subparser.add_argument(
            "--retry",
            type=int,
            default=3,
            metavar="N",
            help="Max retries with exponential backoff on transient errors (default: 3, 0=off)",
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
    login.add_argument(
        "--temp",
        action="store_true",
        help="Do not use 'remember me' (session expires when browser closes)",
    )
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
        "--details",
        action="store_true",
        default=None,
        help="Fetch task detail pages",
    )
    list_parser.add_argument(
        "--view",
        choices=["all", "upcoming", "past", "overdue"],
        default=None,
        help="Restrict output to one view or all views (default: from config, all)",
    )
    
    graded_group = list_parser.add_mutually_exclusive_group()
    graded_group.add_argument(
        "--graded",
        action="store_true",
        default=None,
        help="Show only graded tasks",
    )
    graded_group.add_argument(
        "--not-graded",
        action="store_false",
        dest="graded",
        help="Show only non-graded tasks",
    )

    submitted_group = list_parser.add_mutually_exclusive_group()
    submitted_group.add_argument(
        "--submitted",
        action="store_true",
        default=None,
        help="Show only submitted tasks",
    )
    submitted_group.add_argument(
        "--not-submitted",
        action="store_false",
        dest="submitted",
        help="Show only non-submitted tasks",
    )
    list_parser.add_argument(
        "--grade",
        help="Filter tasks by grade (e.g. 'B', 'B-', '4.0')",
    )
    list_parser.add_argument(
        "--tag", "-t",
        help="Filter tasks by tag/label (e.g. 'Exam', 'Summative')",
    )
    completed_group = list_parser.add_mutually_exclusive_group()
    completed_group.add_argument(
        "--completed",
        action="store_true",
        default=None,
        help="Show only completed tasks (either submitted or passing grade)",
    )
    completed_group.add_argument(
        "--todo",
        action="store_true",
        default=None,
        help="Show only uncompleted/todo tasks (not submitted and ungraded/F)",
    )
    list_parser.add_argument(
        "--deleted",
        action="store_true",
        help="Include tasks that were deleted from the server",
    )
    list_parser.set_defaults(func=cmd_list)

    view = subparsers.add_parser("view", help="View one task in detail")
    add_common_auth_flags(view)
    view.add_argument("target", nargs="?", help="Task id or task URL")
    view.add_argument("--id", help="Task id")
    view.add_argument("--url", help="Task URL")
    view.add_argument("--subject", help="Optional subject filter when resolving by id")
    view.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
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
    logout.set_defaults(func=cmd_logout)

    daemon = subparsers.add_parser("daemon", help="Manage webhook daemon")
    daemon_subparsers = daemon.add_subparsers(dest="daemon_command", required=True)

    daemon_start = daemon_subparsers.add_parser(
        "start", help="Start daemon loop or run one cycle"
    )
    add_common_auth_flags(daemon_start)
    daemon_start.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_start.add_argument("--webhook-url", help="Override webhook URL for this run")
    daemon_start.add_argument(
        "--channel-id", help="Deliver via zeroclaw channel send (e.g. qq, telegram)"
    )
    daemon_start.add_argument(
        "--recipient", help="Channel recipient ID (used with --channel-id)"
    )
    daemon_start.add_argument(
        "--interval", type=int, help="Polling interval in seconds"
    )
    daemon_start.add_argument(
        "--active-hours-start",
        type=int,
        metavar="HOUR",
        help="Start of active hours (0-23, default: 7)",
    )
    daemon_start.add_argument(
        "--active-hours-end",
        type=int,
        metavar="HOUR",
        help="End of active hours (0-23, default: 23)",
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

    daemon_configure_ch = daemon_subparsers.add_parser(
        "configure-channel",
        help="Persist delivery via zeroclaw channel send (no LLM call)",
    )
    daemon_configure_ch.add_argument(
        "channel_id", help="Channel name (e.g. qq, telegram)"
    )
    daemon_configure_ch.add_argument(
        "recipient", help="Recipient ID (platform-specific)"
    )
    daemon_configure_ch.add_argument(
        "--daemon-config", help="Path to daemon JSON config"
    )
    daemon_configure_ch.add_argument("--output", "-o", help="Write output to file")
    daemon_configure_ch.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_configure_ch.set_defaults(func=cmd_daemon_configure_channel)

    submit = subparsers.add_parser("submit", help="Upload a file to a task dropbox")
    add_common_auth_flags(submit)
    submit.add_argument("target", nargs="?", help="Task id or URL")
    submit.add_argument("file", nargs="?", help="File path to upload")
    submit.add_argument("--id", help="Task id")
    submit.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
    )
    submit.set_defaults(func=cmd_submit)

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
        "--read-all",
        action="store_true",
        help="Mark all notifications as read",
    )
    notifications.set_defaults(func=cmd_notifications)

    calendar_p = subparsers.add_parser("calendar", help="View calendar events")
    add_common_auth_flags(calendar_p)
    calendar_p.add_argument("--start", help="Start date (YYYY-MM-DD)")
    calendar_p.add_argument("--end", help="End date (YYYY-MM-DD)")
    calendar_p.add_argument("--today", action="store_true", help="Show today only")
    calendar_p.add_argument("--ical", action="store_true", help="Output raw iCal feed")
    calendar_p.set_defaults(func=cmd_calendar)

    timetable_p = subparsers.add_parser("timetable", help="View weekly timetable")
    add_common_auth_flags(timetable_p)
    timetable_p.add_argument("--date", help="Start date of week (YYYY-MM-DD)")
    timetable_p.add_argument("--today", action="store_true", help="Show this week")
    timetable_p.set_defaults(func=cmd_timetable)

    grades_p = subparsers.add_parser(
        "grades", help="View class grades and expected grade"
    )
    add_common_auth_flags(grades_p)
    grades_p.add_argument("--class-id", help="Class ID (numeric)")
    grades_p.add_argument("--subject", "-s", help="Fuzzy match class name")
    grades_p.set_defaults(func=cmd_grades)

    count_freq_p = subparsers.add_parser(
        "count-grade-freq", help="Count frequency of each grade letter"
    )
    add_common_auth_flags(count_freq_p)
    count_freq_p.add_argument(
        "--subject", "-s", help="Restrict to one class (fuzzy match)"
    )
    count_freq_p.set_defaults(func=cmd_count_grade_freq)

    download_p = subparsers.add_parser(
        "download", help="Download all attachments and submissions for a task"
    )
    add_common_auth_flags(download_p)
    download_p.add_argument("task_id", help="The ID of the task to download attachments/submissions from")
    download_p.add_argument(
        "--output-dir",
        help="Directory to save the files (defaults to task_<id>_<title_slug> in current directory)",
    )
    download_p.add_argument(
        "--no-submissions",
        action="store_true",
        help="Do not download student submissions",
    )
    download_p.add_argument(
        "--no-attachments",
        action="store_true",
        help="Do not download teacher attachments",
    )
    download_p.set_defaults(func=cmd_download)

    return parser


# ── Entry point ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except CommandError as exc:
        payload = error(args.command, exc.code, exc.message)
        print_payload(payload, args.output, getattr(args, "format", None))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
