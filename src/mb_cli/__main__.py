"""CLI entry-point for ``mb`` / ``python -m mb_cli``."""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .auth import build_client
from .client import ManageBacClient, parse_task_url
from . import __version__
from . import keychain
from .config import (
    clear_creds,
    clear_session,
    config_dir,
    load_state,
    resolve_creds_path,
    save_profile,
    save_session,
    warn_on_weak_permissions,
)
from .daemon import (
    DaemonConfig,
    DaemonService,
    ServiceManager,
    WebhookDispatcher,
    configure_channel_send,
    configure_webhook,
    load_daemon_config,
    start_loop,
    stop_daemon,
)
from .daemon import _resolve_secret
from .exceptions import CommandError
from .filters import (
    classify_task_view,
    find_task_by_id,
    matches_subject,
    result_views,
)
from .formatters import error, ok, print_payload
from .notifications import MNNHubClient, hub_for_domain

log = logging.getLogger(__name__)


# ── Client helpers ──────────────────────────────────────────────────────


def _build_client(args, command: str) -> tuple:
    """CLI wrapper: maps argparse namespace to :func:`auth.build_client`."""
    password = getattr(args, "password", None)
    cookie = args.cookie
    if not password and not cookie:
        # Environment fallback for non-interactive/CI use. `tahuti daemon start -b`
        # already hands these to the detached child, so reading them back closes
        # the loop: `MB_CRAWLER_PASSWORD=... tahuti daemon run` needs no prompt.
        # An explicit --password/--cookie still wins over the environment.
        password = os.environ.get("MB_CRAWLER_PASSWORD") or None
        cookie = os.environ.get("MB_CRAWLER_COOKIE") or None
        if not password and not cookie:
            state = load_state(args.profile, args.config, args.session_file)
            if not state.session.cookie or getattr(args, "reauth", False):
                password = getpass.getpass("ManageBac password: ")
    verify = not getattr(args, "no_verify_tls", False)
    return build_client(
        school=args.school,
        domain=args.domain,
        email=args.email,
        password=password,
        cookie=cookie,
        profile=args.profile,
        refresh=getattr(args, "refresh", False),
        reauth=getattr(args, "reauth", False),
        verify=verify,
        cache_ttl=getattr(args, "cache_ttl", None),
        retry=getattr(args, "retry", 3),
        remember=not getattr(args, "temp", False),
        use_keychain=getattr(args, "keychain", None),
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


DEFAULT_SNAPSHOT_PATH = config_dir() / "snapshot.json"


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
        _harden_dir(path.parent)
        # Write to a temp file in the same directory, restrict permissions,
        # then atomically replace — avoids a world-readable window entirely.
        import tempfile
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".snapshot_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        log.warning("Failed to save snapshot: %s", e)


def _harden_dir(path: Path) -> None:
    """Best-effort restrict a directory to the current user."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _snapshot_path(state) -> Path:
    """Return the snapshot path that belongs to *state*'s config directory.

    The snapshot lives beside the config file, so ``--config`` relocates both.
    This is deliberately *not* :data:`DEFAULT_SNAPSHOT_PATH`, which ignores
    ``--config`` and is only a fallback for callers with no state at all.
    """
    return state.config_path.parent / "snapshot.json"


def _set_submission_state(
    snapshot_path: Path,
    task_id: str,
    submitted: bool,
    client: ManageBacClient | None = None,
) -> None:
    """Force a task's submission state in the local snapshot and persist it.

    ``submitted`` and the presence of a submit button are two views of the
    same fact, so they are always written together — a snapshot with
    ``status="submitted"`` but a live submit button would re-offer the upload.

    *client* is forwarded so a status change invalidates the task's cached
    detail pages, exactly as a crawl-detected change would.
    """
    snapshot = load_snapshot(snapshot_path)
    task = find_task_by_id(snapshot, task_id)
    if not task:
        return
    task["status"] = "submitted" if submitted else "not-submitted"
    task["has_submit_button"] = not submitted
    update_snapshot_with_class_tasks(snapshot_path, [task], client=client)


def _redact_daemon_config(config: dict) -> dict:
    """Return a copy of a daemon config safe to echo into CLI/MCP output.

    The HMAC secret must never appear in stdout, an --output file, or a
    journal, since those are far less protected than the config file itself.
    """
    redacted = copy.deepcopy(config)
    for wh in redacted.get("webhooks") or []:
        if isinstance(wh, dict) and wh.get("secret"):
            wh["secret"] = "***redacted***"
    delivery = redacted.get("delivery")
    if isinstance(delivery, dict) and delivery.get("secret"):
        delivery["secret"] = "***redacted***"
    return redacted


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
                        cid, task_id = parse_task_url(class_link)
                        if cid and task_id:
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
    reclassified = _reclassify_tasks(merged_map, now_ref=now_ref)

    return {
        "student_name": new.get("student_name") or old.get("student_name"),
        "school": new.get("school") or old.get("school"),
        "base_url": new.get("base_url") or old.get("base_url"),
        "crawled_at": new.get("crawled_at") or old.get("crawled_at"),
        "upcoming": reclassified["upcoming"],
        "past": reclassified["past"],
        "overdue": reclassified["overdue"],
    }


def _reclassify_tasks(
    tasks: dict[str, dict] | list[dict], now_ref: datetime | None = None
) -> dict[str, list[dict]]:
    """Reclassify tasks into upcoming, past, overdue based on due_date and status."""
    upcoming = []
    past = []
    overdue = []

    task_list = tasks.values() if isinstance(tasks, dict) else tasks
    for t in task_list:
        view = classify_task_view(t, now_ref=now_ref)
        t["view"] = view
        if view == "upcoming":
            upcoming.append(t)
        elif view == "overdue":
            overdue.append(t)
        else:
            past.append(t)

    return {
        "upcoming": upcoming,
        "past": past,
        "overdue": overdue,
    }


def update_snapshot_with_class_tasks(
    snapshot_path: Path,
    class_tasks: list[dict],
    client: ManageBacClient | None = None,
) -> dict:
    """Update snapshot in-place with freshly fetched tasks for a class.

    Merges updated tasks into the snapshot and reclassifies them into
    upcoming, past, and overdue.
    """
    old_snapshot = load_snapshot(snapshot_path)
    merged_map = {}

    for section in ("upcoming", "past", "overdue"):
        for t in old_snapshot.get(section, []):
            tid = t.get("id")
            if tid:
                merged_map[tid] = t

    for t in class_tasks:
        tid = t.get("id")
        if tid:
            old_t = merged_map.get(tid)
            if old_t:
                grade_changed = (
                    old_t.get("grade_letter") != t.get("grade_letter")
                    or old_t.get("grade_score") != t.get("grade_score")
                )
                old_labels = old_t.get("labels") or []
                new_labels = t.get("labels") or []
                labels_changed = (
                    set(old_labels) != set(new_labels)
                    or old_t.get("status") != t.get("status")
                )
                if (grade_changed or labels_changed) and client:
                    class_link = t.get("link") or old_t.get("link") or ""
                    cid, task_id = parse_task_url(class_link)
                    if cid and task_id:
                        client.invalidate_task_cache(cid, task_id)
                        log.info("Task %s state changed; invalidated cached details.", task_id)

                if not t.get("class_name") and old_t.get("class_name"):
                    t["class_name"] = old_t["class_name"]
            merged_map[tid] = t

    reclassified = _reclassify_tasks(merged_map, now_ref=datetime.now())

    base_url = old_snapshot.get("base_url")
    if not base_url and client and hasattr(client, "base") and isinstance(client.base, str):
        base_url = client.base

    updated = {
        "student_name": old_snapshot.get("student_name"),
        "school": old_snapshot.get("school"),
        "base_url": base_url,
        "crawled_at": old_snapshot.get("crawled_at") or datetime.now().isoformat(),
        "upcoming": reclassified["upcoming"],
        "past": reclassified["past"],
        "overdue": reclassified["overdue"],
    }
    save_snapshot(snapshot_path, updated)
    return updated


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
    snapshot_path = _snapshot_path(state)
    old_snapshot = load_snapshot(snapshot_path)

    # Check if we can reuse the snapshot (crawled within last 15 minutes)
    use_cached_snapshot = False
    if old_snapshot and not args.refresh:
        crawled_at_str = old_snapshot.get("crawled_at")
        if crawled_at_str:
            try:
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
        snapshot_path = _snapshot_path(state)
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
        snapshot_path = _snapshot_path(state)
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
        if task.get("link"):
            detail = client.get_task_detail(task["link"], from_hint=False, bypass_cache=args.refresh)
        else:
            detail = {}

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

    # `--subject` narrows an id lookup to the class it is meant to belong to:
    # resolving an id that turns out to live under a different class is a
    # mismatch the caller asked us to rule out, so say so instead of showing
    # the wrong task's detail page.
    subject = getattr(args, "subject", None)
    if subject and not matches_subject(task, subject):
        payload = error(
            "view",
            "subject_mismatch",
            f"Task {task_id} is not in a class matching {subject!r}",
        )
        print_payload(payload, args.output, args.format)
        return 1

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

    # `logout` must actually mean logout: the response cache holds full grade
    # pages and the MNN-hub Bearer JWT, which would otherwise survive.
    cache_cleared = None
    if not getattr(args, "keep_cache", False):
        try:
            import hashlib
            from .cache import DEFAULT_CACHE_DIR, ResponseCache
            email = state.session.email or state.profile.email
            if email:
                email_hash = hashlib.sha256(email.encode()).hexdigest()[:16]
                target = DEFAULT_CACHE_DIR / email_hash
            else:
                target = DEFAULT_CACHE_DIR
            cache_cleared = ResponseCache(cache_dir=target).clear()
        except Exception as e:
            log.warning("Failed to clear response cache on logout: %s", e)

    # `logout` must mean logout for the password too. Leaving creds.json behind
    # would keep the cleartext password on disk after the user asked to be
    # logged out, so it goes by default; --keep-credentials opts back into
    # silent re-login for users who find the prompt more annoying than the risk.
    creds_removed = False
    keychain_removed = False
    if not getattr(args, "keep_credentials", False):
        creds_removed = clear_creds(resolve_creds_path())
        email = state.session.email or state.profile.email
        if email:
            keychain_removed = keychain.delete(email)

    payload = ok(
        "logout",
        state.active_profile,
        {
            "logged_out": True,
            "all_profiles": args.all,
            "cache_entries_removed": cache_cleared,
            "credentials_removed": creds_removed,
            "keychain_entry_removed": keychain_removed,
            "credentials_kept": bool(getattr(args, "keep_credentials", False)),
        },
    )
    print_payload(payload, args.output, args.format)
    return 0


def _apply_daemon_overrides(daemon_config: dict, args) -> dict:
    """Fold the shared daemon CLI flags into a daemon config dict, in place.

    ``daemon run`` and ``daemon start`` expose the same delivery, interval and
    active-window settings, so both resolve them through here. Sharing one code
    path is the only thing that stops the two sibling commands from drifting
    into meaning different things by the same flag name — which is exactly how
    ``--interval`` ended up writing a key nothing reads.
    """
    if getattr(args, "webhook_url", None):
        daemon_config["webhooks"] = [
            {
                "url": args.webhook_url,
                "secret": _resolve_secret(getattr(args, "secret", None)),
                "events": ["*"],
                "enabled": True,
            }
        ]
        daemon_config["delivery"] = {"mode": "webhook", "webhook_url": args.webhook_url}

    channel_id = getattr(args, "channel_id", None)
    recipient = getattr(args, "recipient", None)
    if channel_id and recipient:
        daemon_config["delivery"] = {
            "mode": "channel_send",
            "channel_id": channel_id,
            "recipient": recipient,
        }

    # `run` spells this `--poll-interval`, `start` spells it `--interval`; both
    # names are accepted on both commands, but the config key is the one
    # DaemonConfig.from_dict actually reads.
    interval = getattr(args, "poll_interval", None)
    if interval is None:
        interval = getattr(args, "interval", None)
    if interval is not None:
        daemon_config["poll_interval_seconds"] = interval
    daemon_config.pop("interval", None)

    # Active hours are stored as `active_windows`, matching what
    # load_daemon_config() derives from the same keys in daemon.json.
    hours_start = getattr(args, "active_hours_start", None)
    hours_end = getattr(args, "active_hours_end", None)
    if hours_start is not None or hours_end is not None:
        start = 7 if hours_start is None else hours_start
        end = 23 if hours_end is None else hours_end
        daemon_config["active_windows"] = [[f"{start:02d}:00", f"{end:02d}:00"]]
    daemon_config.pop("active_hours_start", None)
    daemon_config.pop("active_hours_end", None)
    return daemon_config


def cmd_daemon_run(args) -> int:
    state, client, email = _build_client(args, "daemon")
    _authenticate_client(state, client, email)
    daemon_config = load_daemon_config(getattr(args, "daemon_config", None))
    _apply_daemon_overrides(daemon_config, args)
    config = DaemonConfig.from_dict(daemon_config)

    def refresh_fn() -> bool:
        from .auth import _relogin_from_creds
        try:
            _relogin_from_creds(client, state)
            return True
        except Exception as err:
            log.warning("Silent re-login failed: %s", err)
            return False

    service = DaemonService(
        client,
        config=config,
        auth_refresh_fn=refresh_fn,
        dry_run=getattr(args, "dry_run", False),
    )
    if getattr(args, "once", False):
        res = service.run_check_cycle()
        payload = ok("daemon.run", state.active_profile, res)
        print_payload(payload, args.output, args.format)
        return 0
    service.run_forever()
    return 0


def cmd_daemon_start(args) -> int:
    if getattr(args, "background", False):
        mgr = ServiceManager(
            pid_path=getattr(args, "pid_file", None),
            log_path=getattr(args, "log_file", None),
        )
        extra_args = []
        # Secrets go to the child through its environment, never argv: argv is
        # readable by any local user via `ps` for the life of the daemon.
        daemon_secret_env: dict[str, str] = {}
        if getattr(args, "profile", None):
            extra_args.extend(["--profile", args.profile])
        if getattr(args, "config", None):
            extra_args.extend(["--config", args.config])
        if getattr(args, "session_file", None):
            extra_args.extend(["--session-file", args.session_file])
        if getattr(args, "school", None):
            extra_args.extend(["--school", args.school])
        if getattr(args, "domain", None):
            extra_args.extend(["--domain", args.domain])
        if getattr(args, "email", None):
            extra_args.extend(["--email", args.email])
        if getattr(args, "password", None):
            daemon_secret_env["MB_CRAWLER_PASSWORD"] = args.password
        if getattr(args, "cookie", None):
            daemon_secret_env["MB_CRAWLER_COOKIE"] = args.cookie
        if getattr(args, "daemon_config", None):
            extra_args.extend(["--daemon-config", args.daemon_config])
        if getattr(args, "webhook_url", None):
            extra_args.extend(["--webhook-url", args.webhook_url])
        if getattr(args, "channel_id", None) and getattr(args, "recipient", None):
            extra_args.extend(["--channel-id", args.channel_id])
            extra_args.extend(["--recipient", args.recipient])
        if getattr(args, "secret", None):
            daemon_secret_env["MB_WEBHOOK_SECRET"] = args.secret
        interval = getattr(args, "interval", None)
        if interval is None:
            interval = getattr(args, "poll_interval", None)
        if interval is not None:
            extra_args.extend(["--poll-interval", str(interval)])
        if getattr(args, "active_hours_start", None) is not None:
            extra_args.extend(["--active-hours-start", str(args.active_hours_start)])
        if getattr(args, "active_hours_end", None) is not None:
            extra_args.extend(["--active-hours-end", str(args.active_hours_end)])
        # Without forwarding these the detached child would ignore them: `-b
        # --once` would loop forever and `-b --dry-run` would POST webhooks.
        if getattr(args, "once", False):
            extra_args.append("--once")
        if getattr(args, "dry_run", False):
            extra_args.append("--dry-run")
        if getattr(args, "no_verify_tls", False):
            extra_args.append("--no-verify-tls")

        res = mgr.start_background(extra_args=extra_args, env=daemon_secret_env)
        payload = ok("daemon.start", getattr(args, "profile", "default") or "default", res)
        print_payload(payload, args.output, args.format)
        return 0 if res.get("started") else 1

    state, client, email = _build_client(args, "daemon")
    _authenticate_client(state, client, email)
    daemon_config = load_daemon_config(args.daemon_config)
    _apply_daemon_overrides(daemon_config, args)
    result = start_loop(client, daemon_config, dry_run=args.dry_run, once=args.once)
    payload = ok(
        "daemon.start", state.active_profile, result | {"daemon": _redact_daemon_config(daemon_config)}
    )
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_stop(args) -> int:
    mgr = ServiceManager(pid_path=getattr(args, "pid_file", None))
    result = mgr.stop_background()
    if not result.get("stopped") and result.get("reason") == "not_running":
        # Fall back to legacy stop_daemon logic
        result = stop_daemon(getattr(args, "daemon_config", None))
    payload = ok("daemon.stop", "default", result)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_status(args) -> int:
    mgr = ServiceManager(
        pid_path=getattr(args, "pid_file", None),
        log_path=getattr(args, "log_file", None),
    )
    res = mgr.status()
    payload = ok("daemon.status", "default", res)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_daemon_test_webhook(args) -> int:
    url = getattr(args, "url", None)
    secret = getattr(args, "secret", None)
    if not url:
        config = load_daemon_config(getattr(args, "daemon_config", None))
        webhooks = config.get("webhooks", [])
        if webhooks:
            url = webhooks[0].get("url")
            secret = secret or webhooks[0].get("secret")
        else:
            url = config.get("delivery", {}).get("webhook_url")
    if not url:
        raise CommandError("missing_argument", "No webhook URL provided or configured")
    dispatcher = WebhookDispatcher()
    res = dispatcher.test_ping(url, secret=secret)
    payload = ok("daemon.test-webhook", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("success") else 1


def cmd_daemon_install(args) -> int:
    mgr = ServiceManager(log_path=getattr(args, "log_file", None))
    res = mgr.install_service()
    payload = ok("daemon.install", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("installed") else 1


def cmd_daemon_uninstall(args) -> int:
    mgr = ServiceManager()
    res = mgr.uninstall_service()
    payload = ok("daemon.uninstall", "default", res)
    print_payload(payload, args.output, args.format)
    return 0 if res.get("uninstalled") else 1


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
    client: ManageBacClient,
    target: str,
    pages: int = 10,
    snapshot_path: Path | None = None,
) -> tuple[str, str]:
    """Resolve a task target (id, URL, or class/task pair) to (class_id, task_id)."""
    cid, tid = parse_task_url(target)
    if cid and tid:
        return cid, tid
    task_id = tid or target

    # 1. Search local snapshot first for instant resolution
    snap_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if snap_path.exists():
        snapshot = load_snapshot(snap_path)
        task = find_task_by_id(snapshot, task_id)
        if task and task.get("link"):
            cid, tid = parse_task_url(task["link"])
            if cid and tid:
                return cid, tid

    # 2. Fall back to crawling
    result = client.crawl_all(max_pages=pages, fetch_details=False)
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            cid, tid = parse_task_url(task.get("link", ""))
            if cid and tid:
                return cid, tid

    found = client.find_task_by_id(task_id, max_pages=pages)
    if found and found.get("link"):
        cid, tid = parse_task_url(found["link"])
        if cid and tid:
            return cid, tid

    raise CommandError("task_not_found", f"Could not find task with id {task_id}")


def cmd_submit(args) -> int:
    state, client, email = _build_client(args, "submit")
    _authenticate_client(state, client, email)

    # `--id` is the alternate spelling of the positional `target`; sibling
    # commands (`view`, `submissions`) accept both, so honour it here too.
    target = args.target or getattr(args, "id", None)
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

    snapshot_path = _snapshot_path(state)

    try:
        class_id, task_id = _resolve_task_ids(
            client, target, args.pages, snapshot_path=snapshot_path
        )
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

    # Eagerly refresh the class in the snapshot so subsequent commands reflect submission immediately
    try:
        old_snapshot = load_snapshot(snapshot_path)
        existing_task = find_task_by_id(old_snapshot, task_id)
        class_name = existing_task.get("class_name") if existing_task else None

        fresh_tasks = client.get_class_tasks(
            class_id, class_name=class_name, bypass_cache=True
        )
        if fresh_tasks:
            update_snapshot_with_class_tasks(
                snapshot_path, fresh_tasks, client=client
            )
            log.info(
                "Eagerly refreshed snapshot for class %s (%d tasks)",
                class_id,
                len(fresh_tasks),
            )
        elif existing_task:
            _set_submission_state(snapshot_path, task_id, True, client=client)
    except Exception as exc:
        log.warning("Failed to eagerly refresh snapshot after submit: %s", exc)
        try:
            _set_submission_state(snapshot_path, task_id, True, client=client)
        except Exception:
            pass

    payload = ok("submit", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


def cmd_submissions(args) -> int:
    state, client, email = _build_client(args, "submissions")
    _authenticate_client(state, client, email)

    target = args.target or getattr(args, "id", None)
    if not target:
        payload = error(
            "submissions", "missing_target", "Provide a task id or URL"
        )
        print_payload(payload, args.output, args.format)
        return 1

    snapshot_path = _snapshot_path(state)
    pages = getattr(args, "pages", 10)
    try:
        class_id, task_id = _resolve_task_ids(
            client, target, pages, snapshot_path=snapshot_path
        )
    except CommandError as exc:
        payload = error("submissions", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    task_title = None
    try:
        snap = load_snapshot(snapshot_path)
        t_info = find_task_by_id(snap, task_id)
        if t_info:
            task_title = t_info.get("title")
    except Exception:
        pass

    # 1. Action: --add / --submit
    if getattr(args, "add", None):
        file_path = args.add
        try:
            result = client.submit_file(class_id, task_id, file_path)
        except (FileNotFoundError, RuntimeError) as exc:
            payload = error("submissions", "upload_failed", str(exc))
            print_payload(payload, args.output, args.format)
            return 1

        # Eagerly refresh snapshot
        try:
            _set_submission_state(snapshot_path, task_id, True, client=client)
        except Exception:
            pass

        data = {
            "action": "add",
            "task_id": task_id,
            "filename": result.get("filename"),
            "task_url": result.get("task_url"),
        }
        payload = ok("submissions", state.active_profile, data)
        print_payload(payload, args.output, args.format)
        return 0

    # 2. Action: --delete
    if getattr(args, "delete", None):
        asset_ident = args.delete
        try:
            result = client.delete_submission(class_id, task_id, asset_ident)
        except (ValueError, RuntimeError) as exc:
            payload = error("submissions", "delete_failed", str(exc))
            print_payload(payload, args.output, args.format)
            return 1

        # Refresh snapshot: if 0 submissions remaining, mark not-submitted
        try:
            if result.get("remaining_submissions", 0) == 0:
                _set_submission_state(snapshot_path, task_id, False, client=client)
        except Exception:
            pass

        data = {
            "action": "delete",
            "task_id": task_id,
            "filename": result.get("filename"),
            "asset_id": result.get("asset_id"),
            "remaining_submissions": result.get("remaining_submissions", 0),
            "task_url": result.get("task_url"),
        }
        payload = ok("submissions", state.active_profile, data)
        print_payload(payload, args.output, args.format)
        return 0

    # 3. Action: --check-feedback
    if getattr(args, "check_feedback", None) is not None:
        target_asset = (
            args.check_feedback
            if isinstance(args.check_feedback, str)
            else None
        )
        # get_teacher_feedback returns a *dict* (`feedback_items` holds the list),
        # so filtering has to reach into that key — iterating the dict yields
        # only its keys, which is what used to crash with AttributeError.
        feedback_result = client.get_teacher_feedback(class_id, task_id)
        if target_asset:
            items = feedback_result.get("feedback_items") or []
            needle = target_asset.lower()
            matched = []
            for item in items:
                sub_name = (item.get("submission_name") or "").lower()
                att_names = [
                    (a.get("name") or "").lower()
                    for a in (item.get("attachments") or [])
                ]
                if needle in sub_name or any(needle in a for a in att_names):
                    matched.append(item)
            feedback_result = dict(feedback_result)
            feedback_result["feedback_items"] = matched
            feedback_result["feedback_count"] = len(matched)
        payload = ok("feedback", state.active_profile, feedback_result)
        print_payload(payload, args.output, args.format)
        return 0

    # 4. Action: --list or Default (when task ID is provided)
    # `--list` is the explicit spelling of what already happens by default; the
    # flag is read rather than ignored so it is echoed back and a future change
    # to the default cannot silently change what the flag does.
    explicit_list = bool(getattr(args, "list", False))
    submissions = client.get_submissions(class_id, task_id)
    data = {
        "action": "list",
        "task_id": task_id,
        "task_title": task_title,
        "submissions": submissions,
    }
    if explicit_list:
        data["requested"] = "list"
    payload = ok("submissions", state.active_profile, data)
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
    result = hub.list(
        page=args.page,
        per_page=args.per_page,
        filter_="unread" if getattr(args, "unread_only", False) else "all",
    )
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


def _safe_filename(name: str, fallback: str = "download") -> str:
    """Reduce a remotely-supplied filename to a single safe path component.

    Attachment names are scraped from ManageBac HTML, so they must never be
    able to escape the chosen output directory via ``../`` or an absolute
    path.
    """
    raw = str(name or "").strip()
    # Drop any directory component and all path separators.
    base = raw.replace("\\", "/").split("/")[-1]
    base = base.strip().strip(".")
    # Remove NUL and control characters, and anything exotic.
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip()
    if not base or base in (".", ".."):
        return fallback
    return base[:255]


def cmd_download(args) -> int:
    state, client, email = _build_client(args, "download")
    _authenticate_client(state, client, email)

    task_id = args.task_id

    # 1. Look up task in snapshot first
    snapshot_path = _snapshot_path(state)
    snapshot = load_snapshot(snapshot_path)

    task = find_task_by_id(snapshot, task_id)

    if not task:
        log.info("Task %s not found in local snapshot. Searching server...", task_id)
        task = client.find_task_by_id(task_id, max_pages=getattr(args, "pages", 10) or 10)
        if not task:
            payload = error(
                "download", "task_not_found", f"Task {task_id} not found on ManageBac."
            )
            print_payload(payload, args.output, args.format)
            return 1

    link = task.get("link")
    if not link:
        payload = error("download", "no_task_link", f"Task {task_id} has no detail link.")
        print_payload(payload, args.output, args.format)
        return 1

    # 2. Fetch task details
    log.info("Fetching details for task %s...", task_id)
    detail = client.get_task_detail(link, from_hint=False)
    if not detail:
        payload = error(
            "download", "detail_fetch_failed", f"Failed to fetch details for task {task_id}."
        )
        print_payload(payload, args.output, args.format)
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
    attachments = detail.get("attachments", []) or []

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
        # No files is not a failure: an empty dropbox downloads nothing. Emit the
        # structured summary anyway so `--format json` and `--output` still work
        # and callers can tell "nothing to do" from "something went wrong".
        payload = ok(
            "download",
            state.active_profile,
            {
                "task_id": task_id,
                "task_title": task.get("title"),
                "output_dir": str(out_dir),
                "downloaded": [],
                "failed": [],
                "downloaded_count": 0,
                "failed_count": 0,
            },
        )
        print_payload(payload, args.output, args.format)
        return 0

    log.info("Downloading %d file(s) to %s...", len(files_to_download), out_dir)
    # Resolve once so containment can be verified for every file written.
    out_root = out_dir.resolve()
    downloaded: list[dict] = []
    failed: list[dict] = []
    for name, url, source_type in files_to_download:
        safe_name = _safe_filename(name)
        dest_path = out_dir / safe_name
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        counter = 1
        while dest_path.exists():
            dest_path = out_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        # Defence in depth: never write outside the output directory.
        if out_root not in dest_path.resolve().parents:
            log.error("Refusing to write outside %s: %r", out_dir, name)
            failed.append(
                {
                    "name": name,
                    "source": source_type,
                    "reason": "refused_outside_output_dir",
                }
            )
            continue

        log.info("  [%s] Downloading %s...", source_type, name)
        try:
            with client.session.get(url, stream=True) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            log.info("    Saved as %s", dest_path.name)
            downloaded.append(
                {"name": name, "path": str(dest_path), "source": source_type}
            )
        except Exception as e:
            log.error("    Failed to download %s: %s", name, e)
            failed.append(
                {"name": name, "source": source_type, "reason": str(e)}
            )

    log.info(
        "Successfully downloaded %d/%d file(s).", len(downloaded), len(files_to_download)
    )
    payload = ok(
        "download",
        state.active_profile,
        {
            "task_id": task_id,
            "task_title": task.get("title"),
            "output_dir": str(out_dir),
            "downloaded": downloaded,
            "failed": failed,
            "downloaded_count": len(downloaded),
            "failed_count": len(failed),
        },
    )
    print_payload(payload, args.output, args.format)
    # A partial success is still a usable run, so exit 0 unless *nothing* landed.
    return 0 if downloaded else 1


def cmd_feedback(args) -> int:
    """Fetch teacher feedback for all submitted files on a task's dropbox."""
    state, client, email = _build_client(args, "feedback")
    _authenticate_client(state, client, email)

    target = args.task_id
    snapshot_path = _snapshot_path(state)
    try:
        class_id, task_id = _resolve_task_ids(
            client, target, getattr(args, "pages", 10), snapshot_path=snapshot_path
        )
    except CommandError as exc:
        payload = error("feedback", exc.code, exc.message)
        print_payload(payload, args.output, args.format)
        return 1

    result = client.get_teacher_feedback(class_id, task_id)
    payload = ok("feedback", state.active_profile, result)
    print_payload(payload, args.output, args.format)
    return 0


# ── CLI parser ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tahuti",
        description="Crawl ManageBac tasks, grades & submissions",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"tahuti {__version__}",
        help="Show program version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_auth_flags(subparser, include_password: bool = True) -> None:
        subparser.add_argument(
            "--profile",
            default=None,
            help="Profile name (default: active_profile or default)",
        )
        subparser.add_argument("--config", help="Path to config JSON")
        subparser.add_argument("--session-file", help="Path to session JSON")
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
    login.add_argument(
        "--keychain",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Store the password in the OS keychain instead of cleartext "
        "creds.json (macOS Keychain / Linux secret-tool). Overrides "
        "MB_CRAWLER_KEYCHAIN.",
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
    logout.add_argument("--config", help="Path to config JSON")
    logout.add_argument("--session-file", help="Path to session JSON")
    logout.add_argument("--all", action="store_true", help="Remove all saved sessions")
    logout.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep the on-disk response cache (it holds grade pages and a hub JWT)",
    )
    logout.add_argument(
        "--keep-credentials",
        action="store_true",
        help="Keep the saved password so later commands can log in silently "
        "(by default `logout` deletes creds.json and any keychain entry)",
    )
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

    daemon_run = daemon_subparsers.add_parser(
        "run", help="Run real-time notification daemon loop in foreground"
    )
    add_common_auth_flags(daemon_run)
    daemon_run.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_run.add_argument("--webhook-url", help="Webhook destination URL")
    daemon_run.add_argument("--secret", help="HMAC secret for webhook signatures")
    # `--interval` is the spelling `daemon start` uses for the same setting; both
    # names resolve to one dest so neither sibling can drift out of sync again.
    daemon_run.add_argument(
        "--poll-interval",
        "--interval",
        dest="poll_interval",
        type=int,
        help="Poll interval in seconds (alias: --interval)",
    )
    daemon_run.add_argument(
        "--channel-id", help="Deliver via zeroclaw channel send (e.g. qq, telegram)"
    )
    daemon_run.add_argument(
        "--recipient", help="Channel recipient ID (used with --channel-id)"
    )
    daemon_run.add_argument(
        "--active-hours-start",
        type=int,
        metavar="HOUR",
        help="First hour the daemon polls, 0-23 local time (default: 7); "
        "outside the active window it sleeps instead of polling",
    )
    daemon_run.add_argument(
        "--active-hours-end",
        type=int,
        metavar="HOUR",
        help="Last hour the daemon polls, 0-23 local time (default: 23)",
    )
    daemon_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not POST webhook, only compute alerts",
    )
    daemon_run.add_argument("--once", action="store_true", help="Run one cycle and exit")
    daemon_run.set_defaults(func=cmd_daemon_run)

    daemon_start = daemon_subparsers.add_parser(
        "start", help="Start daemon loop or run one cycle"
    )
    add_common_auth_flags(daemon_start)
    daemon_start.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_start.add_argument("--webhook-url", help="Override webhook URL for this run")
    daemon_start.add_argument(
        "--secret", help="HMAC secret for webhook signatures"
    )
    daemon_start.add_argument(
        "--channel-id", help="Deliver via zeroclaw channel send (e.g. qq, telegram)"
    )
    daemon_start.add_argument(
        "--recipient", help="Channel recipient ID (used with --channel-id)"
    )
    daemon_start.add_argument(
        "--interval",
        "--poll-interval",
        dest="interval",
        type=int,
        help="Polling interval in seconds (alias: --poll-interval)",
    )
    daemon_start.add_argument(
        "--active-hours-start",
        type=int,
        metavar="HOUR",
        help="First hour the daemon polls, 0-23 local time (default: 7); "
        "outside the active window it sleeps instead of polling",
    )
    daemon_start.add_argument(
        "--active-hours-end",
        type=int,
        metavar="HOUR",
        help="Last hour the daemon polls, 0-23 local time (default: 23)",
    )
    daemon_start.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not POST webhook, only compute alerts",
    )
    daemon_start.add_argument(
        "--once", action="store_true", help="Run one cycle and exit"
    )
    daemon_start.add_argument(
        "--background", "-b", action="store_true", help="Run as detached background process"
    )
    daemon_start.add_argument("--pid-file", help="Custom PID file path")
    daemon_start.add_argument("--log-file", help="Custom log file path")
    daemon_start.set_defaults(func=cmd_daemon_start)

    daemon_stop = daemon_subparsers.add_parser("stop", help="Stop daemon loop")
    daemon_stop.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_stop.add_argument("--pid-file", help="Custom PID file path")
    daemon_stop.add_argument("--output", "-o", help="Write output to file")
    daemon_stop.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_stop.set_defaults(func=cmd_daemon_stop)

    daemon_status = daemon_subparsers.add_parser("status", help="Show daemon process status")
    daemon_status.add_argument("--pid-file", help="Custom PID file path")
    daemon_status.add_argument("--log-file", help="Custom log file path")
    daemon_status.add_argument("--output", "-o", help="Write output to file")
    daemon_status.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_status.set_defaults(func=cmd_daemon_status)

    daemon_test_wh = daemon_subparsers.add_parser("test-webhook", help="Test webhook endpoint with ping event")
    daemon_test_wh.add_argument("url", nargs="?", help="Webhook URL to test")
    daemon_test_wh.add_argument("--secret", help="Optional HMAC secret")
    daemon_test_wh.add_argument("--daemon-config", help="Path to daemon JSON config")
    daemon_test_wh.add_argument("--output", "-o", help="Write output to file")
    daemon_test_wh.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_test_wh.set_defaults(func=cmd_daemon_test_webhook)

    daemon_install = daemon_subparsers.add_parser("install", help="Install auto-start system service (launchd/systemd)")
    daemon_install.add_argument("--log-file", help="Custom log file path")
    daemon_install.add_argument("--output", "-o", help="Write output to file")
    daemon_install.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_install.set_defaults(func=cmd_daemon_install)

    daemon_uninstall = daemon_subparsers.add_parser("uninstall", help="Uninstall auto-start system service")
    daemon_uninstall.add_argument("--output", "-o", help="Write output to file")
    daemon_uninstall.add_argument(
        "--format",
        choices=["pretty", "json"],
        default=None,
        help="Output format (default: pretty for TTY, json otherwise)",
    )
    daemon_uninstall.set_defaults(func=cmd_daemon_uninstall)

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

    submissions_p = subparsers.add_parser(
        "submissions",
        help="Manage task submissions (list, add, delete, check-feedback)",
    )
    add_common_auth_flags(submissions_p)
    submissions_p.add_argument("target", nargs="?", help="Task id or URL")
    submissions_p.add_argument("--id", help="Task id")
    submissions_p.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id",
    )
    submissions_p.add_argument(
        "--list", action="store_true", help="List current submissions for the task"
    )
    submissions_p.add_argument(
        "--add", "--submit", dest="add", help="Upload a file to the task dropbox"
    )
    submissions_p.add_argument(
        "--delete", help="Delete a submitted file by asset ID or filename"
    )
    submissions_p.add_argument(
        "--check-feedback",
        nargs="?",
        const=True,
        default=None,
        help="Check teacher feedback (optionally filter by asset ID or name)",
    )
    submissions_p.set_defaults(func=cmd_submissions)

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
    notifications.add_argument(
        "--unread-only",
        action="store_true",
        help="Only list unread notifications",
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
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id (default: 10)",
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

    feedback_p = subparsers.add_parser(
        "feedback", help="Fetch teacher feedback for a submitted task"
    )
    add_common_auth_flags(feedback_p)
    feedback_p.add_argument(
        "task_id",
        help="Task numeric ID or full ManageBac URL",
    )
    feedback_p.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Max pages to search when resolving by id (default: 10)",
    )
    feedback_p.set_defaults(func=cmd_feedback)

    return parser


# ── Entry point ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    # File permissions are the only barrier protecting a cleartext creds.json,
    # so say so loudly if something outside `mb` loosened them.
    warn_on_weak_permissions()
    try:
        raise SystemExit(args.func(args))
    except CommandError as exc:
        payload = error(args.command, exc.code, exc.message)
        print_payload(payload, args.output, getattr(args, "format", None))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
