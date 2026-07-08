"""MCP server for ManageBac — stdio transport."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from .auth import build_client
from .client import ManageBacClient
from .notifications import MNNHubClient, hub_for_domain

log = logging.getLogger(__name__)

mcp = FastMCP(
    "mb-cli",
    instructions=(
        "ManageBac MCP server. Provides tools to interact with ManageBac: "
        "list/view tasks, submit files, view notifications, calendar events, "
        "timetables, and class grades."
    ),
)


# ── Tasks ───────────────────────────────────────────────────────────────


@mcp.tool()
def list_tasks(
    view: str = "all",
    subject: str | None = None,
    graded: bool | None = None,
    submitted: bool | None = None,
    grade: str | None = None,
    tag: str | None = None,
    details: bool = False,
    pages: int = 10,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """List ManageBac tasks (upcoming, past, overdue).

    Args:
        view: "all", "upcoming", "past", or "overdue"
        subject: Filter by subject/class name (case-insensitive substring)
        graded: Filter by graded status (True=graded only, False=not graded only)
        submitted: Filter by submission status (True=submitted only, False=not submitted only)
        grade: Filter by specific grade letter or GPA (e.g. 'B', 'B-', '4.0')
        details: Fetch task detail pages (slower, one request per task)
        pages: Max pages per view (default 10)
        school: School subdomain (e.g. "bj80")
        domain: Base domain ("managebac.com" or "managebac.cn")
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    
    upcoming = []
    past = []
    overdue = []

    if view in ("upcoming", "all"):
        log.info("Crawling upcoming tasks...")
        upcoming = client.get_tasks_by_view("upcoming", max_pages=pages)
    if view in ("past", "all"):
        log.info("Crawling past tasks...")
        past = client.get_tasks_by_view("past", max_pages=pages)
    if view in ("overdue", "all"):
        log.info("Crawling overdue tasks...")
        overdue = client.get_tasks_by_view("overdue", max_pages=pages)

    if details:
        items = [t for t in upcoming + past + overdue if t.get("link")]
        log.info("Fetching details for %d tasks...", len(items))
        for i, task in enumerate(items):
            detail = client.get_task_detail(task["link"])
            if detail:
                task["detail"] = detail
            if (i + 1) % 5 == 0:
                log.info("  detail %d/%d", i + 1, len(items))
            if i < len(items) - 1:
                time.sleep(random.uniform(0.5, 2.0))

    if subject:
        def _match(task, s):
            cn = task.get("class_name", "")
            return s.lower() in cn.lower() if cn else False

        upcoming = [t for t in upcoming if _match(t, subject)]
        past = [t for t in past if _match(t, subject)]
        overdue = [t for t in overdue if _match(t, subject)]

    from .filters import matches_graded, matches_submitted, matches_grade_query

    if graded is not None:
        upcoming = [t for t in upcoming if matches_graded(t, graded)]
        past = [t for t in past if matches_graded(t, graded)]
        overdue = [t for t in overdue if matches_graded(t, graded)]

    if submitted is not None:
        upcoming = [t for t in upcoming if matches_submitted(t, submitted)]
        past = [t for t in past if matches_submitted(t, submitted)]
        overdue = [t for t in overdue if matches_submitted(t, submitted)]

    if grade is not None:
        upcoming = [t for t in upcoming if matches_grade_query(t, grade)]
        past = [t for t in past if matches_grade_query(t, grade)]
        overdue = [t for t in overdue if matches_grade_query(t, grade)]

    if tag is not None:
        from .filters import matches_tag
        upcoming = [t for t in upcoming if matches_tag(t, tag)]
        past = [t for t in past if matches_tag(t, tag)]
        overdue = [t for t in overdue if matches_tag(t, tag)]

    from datetime import datetime
    result = {
        "student_name": client.student_name,
        "school": client.school,
        "base_url": client.base,
        "crawled_at": datetime.now().isoformat(),
        "upcoming": upcoming,
        "past": past,
        "overdue": overdue,
        "summary": {
            "upcoming_count": len(upcoming),
            "past_count": len(past),
            "overdue_count": len(overdue),
        },
    }

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def view_task(
    task_id: str | None = None,
    task_url: str | None = None,
    pages: int = 10,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """View detailed information about a specific task.

    Provide either task_id or task_url. The task_url can be a full ManageBac URL.

    Args:
        task_id: Numeric task ID (e.g. "27254393")
        task_url: Full task URL
        pages: Max pages to search when resolving by id
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    target = task_url or task_id
    if not target:
        return json.dumps({"error": "Provide task_id or task_url"})

    detail = client.get_task_detail(target)
    task = {"id": target.split("core_tasks/")[-1].split("/")[0], "link": target}
    return json.dumps({"task": task, "detail": detail}, indent=2, ensure_ascii=False)


# ── File submission ─────────────────────────────────────────────────────


@mcp.tool()
def submit_file(
    task_id: str,
    file_path: str,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Submit a file to a task's dropbox.

    The task_id can be a numeric ID or a full ManageBac URL.
    PREFER passing the full task URL (e.g. from the list_tasks results) to bypass resolution.

    Args:
        task_id: Task ID or full URL (e.g. "27254393" or "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393")
        file_path: Local path to the file to upload
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", task_id)
    if m:
        class_id, tid = m.group(1), m.group(2)
    else:
        found = False
        # Search upcoming and overdue first (most likely for submissions)
        for view in ("upcoming", "overdue"):
            tasks = client.get_tasks_by_view(view, max_pages=3)
            for t in tasks:
                if t.get("id") == task_id:
                    m2 = re.search(
                        r"/student/classes/(\d+)/core_tasks/(\d+)", t.get("link", "")
                    )
                    if m2:
                        class_id, tid = m2.group(1), m2.group(2)
                        found = True
                        break
            if found:
                break

        if not found:
            # Fall back to past tasks
            tasks = client.get_tasks_by_view("past", max_pages=3)
            for t in tasks:
                if t.get("id") == task_id:
                    m2 = re.search(
                        r"/student/classes/(\d+)/core_tasks/(\d+)", t.get("link", "")
                    )
                    if m2:
                        class_id, tid = m2.group(1), m2.group(2)
                        found = True
                        break

        if not found:
            return json.dumps({"error": f"Task {task_id} not found"})

    try:
        result = client.submit_file(class_id, tid, file_path)
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Notifications ───────────────────────────────────────────────────────


@mcp.tool()
def get_notifications(
    page: int = 1,
    per_page: int = 20,
    unread_only: bool = False,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch notifications from ManageBac.

    Args:
        page: Page number (default 1)
        per_page: Items per page (default 20)
        unread_only: Only show unread notifications
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub_endpoint, token = client.get_notification_token()
    if not hub_endpoint:
        hub_endpoint = hub_for_domain(client.domain)
    hub = MNNHubClient(hub_endpoint, token)

    stats = hub.stats()
    filter_ = "unread" if unread_only else "all"
    result = hub.list(page=page, per_page=per_page, filter_=filter_)
    return json.dumps(
        {"stats": stats, "items": result["items"], "meta": result["meta"]},
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def mark_notification(
    notification_id: int,
    action: str = "read",
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Mark a notification as read, unread, starred, or unstarred.

    Args:
        notification_id: Numeric notification ID
        action: "read", "unread", "star", or "unstar"
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub_endpoint, token = client.get_notification_token()
    if not hub_endpoint:
        hub_endpoint = hub_for_domain(client.domain)
    hub = MNNHubClient(hub_endpoint, token)

    actions = {
        "read": hub.mark_read,
        "unread": hub.mark_unread,
        "star": hub.star,
        "unstar": hub.unstar,
    }
    fn = actions.get(action)
    if not fn:
        return json.dumps(
            {"error": f"Unknown action: {action}. Use read/unread/star/unstar"}
        )

    ok = fn(notification_id)
    return json.dumps({"ok": ok, "notification_id": notification_id, "action": action})


@mcp.tool()
def mark_all_notifications_read(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Mark all notifications as read.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    hub_endpoint, token = client.get_notification_token()
    if not hub_endpoint:
        hub_endpoint = hub_for_domain(client.domain)
    hub = MNNHubClient(hub_endpoint, token)
    ok = hub.mark_all_read()
    return json.dumps({"ok": ok, "action": "mark_all_read"})


# ── Calendar ────────────────────────────────────────────────────────────


@mcp.tool()
def get_calendar_events(
    start_date: str | None = None,
    end_date: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch calendar events for a date range.

    Dates are YYYY-MM-DD strings. Defaults to today through +6 days.

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    today = date.today()
    start = start_date or today.isoformat()
    end = end_date or (today + timedelta(days=6)).isoformat()
    events = client.get_calendar_events(start, end)
    return json.dumps(
        {"start": start, "end": end, "events": events}, indent=2, ensure_ascii=False
    )


@mcp.tool()
def get_ical_feed(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch the raw iCal feed for the calendar.

    Returns the iCal text content. Parse with an iCal library to extract events.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    return client.get_ical_feed()


# ── Timetable ───────────────────────────────────────────────────────────


@mcp.tool()
def get_timetable(
    date_str: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Fetch the weekly timetable.

    Args:
        date_str: Start date of week (YYYY-MM-DD). Defaults to this week.
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    result = client.get_timetable(date_str)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Grades ──────────────────────────────────────────────────────────────


@mcp.tool()
def list_classes(
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """List all classes for the current student with their IDs.

    Use this to find the class_id for get_class_grades.

    Args:
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    result = client.crawl_all(max_pages=5, fetch_details=False)
    seen: dict[str, str] = {}
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        link = task.get("link", "")
        m = re.search(r"/student/classes/(\d+)/", link)
        cname = task.get("class_name", "")
        if m and cname:
            seen[m.group(1)] = cname
    classes = [{"id": cid, "name": cname} for cid, cname in seen.items()]
    return json.dumps({"classes": classes}, indent=2, ensure_ascii=False)


@mcp.tool()
def get_class_grades(
    class_id: str | None = None,
    class_name: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Get all grades for a class with expected grade calculation.

    Provide either class_id (numeric) or class_name (fuzzy substring match).

    Args:
        class_id: Numeric class ID (e.g. "11460711")
        class_name: Fuzzy match class name (e.g. "EL" matches "CAIE IGCSE G9 EL-L0")
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )

    if not class_id and class_name:
        result = client.crawl_all(max_pages=5, fetch_details=False)
        seen: dict[str, str] = {}
        for task in result["upcoming"] + result["past"] + result["overdue"]:
            link = task.get("link", "")
            m = re.search(r"/student/classes/(\d+)/", link)
            cname = task.get("class_name", "")
            if m and cname:
                seen[m.group(1)] = cname
        for cid, cname in seen.items():
            if class_name.lower() in cname.lower():
                class_id = cid
                break
        if not class_id:
            return json.dumps(
                {
                    "error": f"No class matching '{class_name}'",
                    "available": list(seen.values()),
                }
            )

    if not class_id:
        # Default to loading grades for all classes
        result = client.crawl_all(max_pages=5, fetch_details=False)
        seen = {}
        for task in result["upcoming"] + result["past"] + result["overdue"]:
            link = task.get("link", "")
            m = re.search(r"/student/classes/(\d+)/", link)
            cname = task.get("class_name", "")
            if m and cname:
                seen[m.group(1)] = cname
        
        all_grades = {}
        for cid, cname in seen.items():
            try:
                c_grades = client.get_class_grades(cid)
                c_grades["class_name"] = cname
                all_grades[cid] = c_grades
            except Exception as e:
                log.warning("failed to fetch grades for class %s: %s", cid, e)
        return json.dumps({"classes_grades": all_grades}, indent=2, ensure_ascii=False)

    grades = client.get_class_grades(class_id)
    grades["class_id"] = class_id
    return json.dumps(grades, indent=2, ensure_ascii=False)


# ── Grade frequency ─────────────────────────────────────────────────────


@mcp.tool()
def count_grade_frequencies(
    class_name: str | None = None,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Count frequency of each grade letter across all or one class.

    Args:
        class_name: Fuzzy match class name (omit for all classes)
        school: School subdomain
        domain: Base domain
        cookie: Session cookie override
        profile: Profile name
        verify_tls: Set to False to disable TLS certificate verification
        retry: Max retries with exponential backoff (default 3, 0=off)
    """
    _state, client, _email = build_client(
        school=school,
        domain=domain,
        cookie=cookie,
        profile=profile,
        verify=verify_tls,
        retry=retry,
    )
    result = client.count_grade_frequencies(class_filter=class_name)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Entry point ─────────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
