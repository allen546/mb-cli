"""Main daemon service orchestrating provider polling, task enrichment, scheduling, and dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
import hashlib
import logging
import os
from pathlib import Path
import random
import signal
import time
from typing import Any

from ..client import ManageBacClient, parse_due_date
from ..task_status import (
    format_grade_display,
    is_task_graded,
    is_task_submitted_or_graded,
)
from .events import DaemonConfig, MBEvent, standardize_task_payload
from .provider import AbstractNotificationProvider, MNNHubProvider
from .scheduler import DDLScheduler, resolve_school_timezone
from .state import DaemonStateManager
from .stealth import StealthTaskCrawler
from .system import write_pid_file
from .webhook import WebhookDispatcher, retryable_results

log = logging.getLogger(__name__)

# Floor on the full upcoming-view re-crawl. The daemon's whole appeal is stealth
# jitter; a scraper that re-crawls every poll interval is a scraper that gets
# rate-limited.
FULL_SYNC_MIN_INTERVAL_SECONDS = 43200  # 12h
# First retry delay after a *failed* sync, doubling per consecutive failure and
# capped at the full sync interval.
SYNC_RETRY_BASE_SECONDS = 300


def _safe_log(value: Any) -> str:
    """Strip control characters from a remotely-sourced string before logging."""
    return "".join(
        ch for ch in str(value or "") if ch == "\t" or (0x20 <= ord(ch) != 0x7F)
    )[:200]


def _notification_dedup_key(raw_id: Any) -> int | None:
    """Coerce a remote ``notification_id`` into an int dedup key.

    Returns None for anything that is not a whole number. Doing this per event
    is what keeps one malformed id from poisoning the whole cycle: the previous
    bare ``int(notif_id)`` raised inside the polling loop, and the loop's only
    guard was a blanket ``except`` that abandoned every remaining event.
    """
    if raw_id is None or isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, float):
        return int(raw_id) if raw_id.is_integer() else None
    if isinstance(raw_id, str):
        text = raw_id.strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError:
            return None
    return None


def _synthetic_notification_key(event: MBEvent) -> str:
    """Stable stand-in id for a notification that carries no usable one.

    ``fetch_raw_notifications`` re-reads the same page-1 window every cycle, so
    an event that is never recorded as processed comes back forever and gets
    re-POSTed every time. The key is a hash of the event's own content, which is
    stable for as long as the underlying notification is unchanged.
    """
    data = event.data if isinstance(event.data, dict) else {}
    parts = [str(event.event)]
    for field in (
        "raw_event_name",
        "title",
        "task_title",
        "created_at",
        "task_id",
        "class_id",
        "body_preview",
    ):
        parts.append(str(data.get(field) or ""))
    digest = hashlib.sha256(
        "|".join(parts).encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"syn_{digest}"


class DaemonService:
    """Core daemon service running the real-time notification loop."""

    def __init__(
        self,
        client: ManageBacClient,
        config: DaemonConfig | None = None,
        state_manager: DaemonStateManager | None = None,
        provider: AbstractNotificationProvider | None = None,
        auth_refresh_fn: Callable[[], bool] | None = None,
        on_start: Callable[[DaemonService], None] | None = None,
        dry_run: bool = False,
        daemon_config: dict | None = None,
        pid_file: str | Path | None = None,
    ):
        self.client = client
        self.config = config or DaemonConfig()
        # A dry run must not persist *anything*, not just webhooks. Two separate
        # side effects make it observable: the dispatcher POSTing, and the state
        # manager recording that a notification was handled. The second is the
        # dangerous one — `mark_notification_processed` followed by `save()` is
        # what makes a notification invisible to the *next* run, so a dry run
        # that persisted it would silently swallow the real delivery it was only
        # meant to preview. Suppressing only the POST left that hole open.
        self.dry_run = dry_run
        # Also cleared on an injected manager, so the guarantee holds whatever
        # the caller built: `persist` is what `save()` consults, and nothing else
        # in the daemon writes to that file.
        if state_manager is not None:
            state_manager.persist = not dry_run
        self.state_manager = state_manager or DaemonStateManager(
            persist=not self.dry_run
        )
        self.auth_refresh_fn = auth_refresh_fn
        self.provider = provider or MNNHubProvider(
            self.client, auth_refresh_fn=self.auth_refresh_fn
        )
        # The raw daemon.json dict carries settings DaemonConfig does not model
        # (it drops unknown keys), so it is threaded through separately.
        self.daemon_config: dict = dict(daemon_config or {})
        school_tz = self.daemon_config.get("school_timezone") or self.daemon_config.get(
            "timezone"
        )
        self.school_timezone = resolve_school_timezone(school_tz)
        self.stealth_crawler = StealthTaskCrawler(self.client, self.config.stealth)
        self.scheduler = DDLScheduler(
            self.state_manager,
            self.config.reminders,
            submission_checker=self._check_is_task_submitted_or_graded,
            school_timezone=self.school_timezone,
        )
        # `--dry-run` has to reach the dispatcher too: the loop only computes
        # what it *would* POST, so a dry run that still POSTed would be worse
        # than no flag at all. An empty webhook list makes `dispatch` a no-op
        # that still reports success, which is the other half of the dry-run
        # contract.
        self.dispatcher = WebhookDispatcher(
            webhooks=[] if self.dry_run else self.config.webhooks,
            verify_tls=self.config.verify_tls,
        )
        self.on_start: Callable[[DaemonService], None] = on_start or (
            lambda svc: svc.sync_upcoming_tasks()
        )
        self._running = False
        self._last_full_sync: float = 0.0
        # When the loop pid file is set, `start` records the running pid so
        # `daemon status`/`daemon stop` can find this process. `daemon run` is
        # the ExecStart of the generated systemd unit and launchd plist, and
        # without this an installed daemon cannot be stopped at all.
        self.pid_file = Path(pid_file).expanduser() if pid_file else None
        # Attempt bookkeeping for the full sync gate: measuring the interval from
        # the last *success* let an empty or failed sync re-crawl every cycle.
        self._last_sync_attempt: float = 0.0
        self._sync_failures: int = 0

    # ── pid file lifecycle ──────────────────────────────────────────────

    def _acquire_pid_file(self) -> None:
        if self.pid_file is None:
            return
        try:
            write_pid_file(self.pid_file)
            log.info("Recorded daemon pid %s in %s", os.getpid(), self.pid_file)
        except OSError as exc:
            log.warning("Could not write pid file %s: %s", self.pid_file, exc)

    def _release_pid_file(self) -> None:
        """Remove our pid file, but only while it still names this process."""
        if self.pid_file is None:
            return
        try:
            current = self.pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if current == str(os.getpid()):
            self.pid_file.unlink(missing_ok=True)

    def _in_active_window(self) -> bool:
        """Whether local time is inside one of the configured active windows.

        No windows configured means always active. A malformed window fails
        *open* — a notifier that keeps polling is a better failure mode than one
        that silently stops because of a typo in daemon.json.
        """
        windows = self.config.active_windows
        if not windows:
            return True
        # Imported at call time: the package's `__init__` imports this module, so
        # a module-level import would be circular.
        from . import _is_in_window, _now_local, _parse_window

        now = _now_local().time()
        try:
            return any(_is_in_window(now, *_parse_window(w)) for w in windows)
        except Exception as exc:
            # Not just ValueError/TypeError/IndexError: a window of the wrong
            # *type* (`[[7, 23]]`) survives DaemonConfig.from_dict's shape filter
            # and dies on `w[0].split(":")` with an AttributeError, which used to
            # escape the `while self._running` loop and kill the process — a
            # 30-second crash loop under the generated unit's Restart=on-failure.
            log.warning(
                "Ignoring malformed active_windows %r (%s) — polling continuously",
                windows,
                exc,
            )
            return True

    def _sleep_until_active_window(self) -> None:
        """Sleep, in slices, until the next active window opens."""
        from . import _next_active_window, _time_until

        window_config = {"active_windows": self.config.active_windows}
        while self._running and not self._in_active_window():
            wait = _time_until(_next_active_window(window_config))
            log.info("Outside active hours — sleeping %.0fs until the next window", wait)
            deadline = time.time() + min(wait, 600)
            while self._running and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))

    def sync_upcoming_tasks(self) -> int:
        """Fetch all upcoming tasks and populate in-memory deadline state."""
        log.info("Performing upcoming tasks sync...")
        # Stamp the attempt before the crawl: measuring the interval from the
        # last *success* is what turned an empty result into a per-cycle crawl.
        self._last_sync_attempt = time.time()
        self._sync_failures += 1
        try:
            upcoming_tasks = self.client.get_tasks_by_view("upcoming", max_pages=3)
        except Exception as exc:
            if ("Session expired" in str(exc) or "login" in str(exc).lower()) and self.auth_refresh_fn:
                log.info("Session expired during upcoming task sync — attempting auto-relogin...")
                if self.auth_refresh_fn():
                    try:
                        upcoming_tasks = self.client.get_tasks_by_view("upcoming", max_pages=3)
                    except Exception as inner_exc:
                        log.warning("Task sync error after re-login: %s", inner_exc)
                        return 0
                else:
                    log.warning("Re-login failed during task sync")
                    return 0
            else:
                log.warning("Task sync error: %s", exc)
                return 0

        synced_count = 0
        for t in upcoming_tasks:
            task_id = t.get("id")
            if not task_id:
                continue
            is_new = self.state_manager.get_task(task_id) is None
            self.state_manager.update_task(t)
            if is_new or is_task_submitted_or_graded(t):
                self._suppress_past_milestones(t)
            synced_count += 1
        self._last_full_sync = time.time()
        self._sync_failures = 0
        self.state_manager.last_synced_at = datetime.now(timezone.utc).isoformat()
        self.state_manager.prune_old_tasks()
        self.state_manager.save()
        log.info("Synced %d upcoming tasks into scheduler", synced_count)
        return synced_count

    def _full_sync_due(self, now: float) -> bool:
        """Whether the upcoming view should be re-crawled on this iteration.

        The gate used to be ``not tasks_cache or interval_elapsed``. ``not
        tasks_cache`` short-circuited before the interval check, so an empty or
        failed sync made the condition true on *every* iteration: a 12-hour
        cadence degraded into a full multi-page crawl every poll interval.
        """
        interval_minutes = self.config.full_sync_interval_minutes
        if interval_minutes <= 0:
            # Periodic sync explicitly disabled.
            return False
        if self._last_sync_attempt <= 0.0:
            return True
        interval = max(
            FULL_SYNC_MIN_INTERVAL_SECONDS, interval_minutes * 60
        )
        elapsed = now - self._last_sync_attempt
        if self._sync_failures == 0:
            return elapsed >= interval
        # The last attempt failed. Back off exponentially so a transient error
        # recovers without hammering, capped at the full interval.
        backoff = min(
            SYNC_RETRY_BASE_SECONDS * (2 ** max(0, self._sync_failures - 1)),
            interval,
        )
        return elapsed >= backoff

    def _suppress_past_milestones(self, task: dict[str, Any]) -> None:
        """Suppress reminder milestones that were already in the past or if task is submitted/graded."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id:
            return

        if is_task_submitted_or_graded(task):
            for th in self.scheduler.reminders:
                self.state_manager.mark_reminder_dispatched(task_id, th.name)
            return

        due_str = task.get("due_date")
        if not due_str:
            return
        due_dt = parse_due_date(due_str)
        if not due_dt:
            return
        if due_dt.tzinfo is None:
            # Same assumption the scheduler makes: a naive due date is
            # school-local wall clock, so measure "now" on the same clock.
            assumed = self.school_timezone or datetime.now().astimezone().tzinfo
            if assumed is not None:
                due_dt = due_dt.replace(tzinfo=assumed)
        now = datetime.now(due_dt.tzinfo) if due_dt.tzinfo else datetime.now()
        minutes_left = (due_dt - now).total_seconds() / 60.0
        for th in self.scheduler.reminders:
            if minutes_left < th.threshold_minutes:
                self.state_manager.mark_reminder_dispatched(task_id, th.name)

    def _check_is_task_submitted_or_graded(self, class_id: str, task_id: str) -> bool:
        """Targeted check: verify task page badge, submission, and grading status before firing an alarm."""
        task = self.state_manager.get_task(task_id)
        if task and is_task_submitted_or_graded(task):
            return True

        try:
            # 1. Check live task page via stealth crawler (finds badges, status, grade)
            details = self.stealth_crawler.fetch_task_details(class_id, task_id)
            if details:
                self.state_manager.update_task(details)
                if is_task_submitted_or_graded(details):
                    return True
        except Exception as e:
            log.debug("Live task page check error for task %s: %s", task_id, e)

        try:
            # 2. Check dropbox table as fallback
            submissions = self.client.get_submissions(class_id, task_id)
            if submissions and not submissions[0].get("error"):
                if task:
                    task["status"] = "submitted"
                    self.state_manager.update_task(task)
                return True
        except Exception as e:
            log.debug("Targeted dropbox check error for task %s: %s", task_id, e)

        return False

    _check_is_task_submitted = _check_is_task_submitted_or_graded

    def _fix_event_id(self, event: MBEvent) -> None:
        """Give an id-less notification a stable ``event_id``.

        ``MNNHubProvider.normalize_notification`` builds
        ``event_id=f"notif_{notif_id}"``, which collapses to ``"notif_None"``
        for every notification without an id — one shared string, useless to a
        receiver-side dedup. Deriving it from the content instead means the
        receiver can drop the repeat even if we somehow send it.
        """
        current = str(getattr(event, "event_id", "") or "")
        if not current or current.endswith("_None") or current in ("notif_None", "notif_"):
            event.event_id = _synthetic_notification_key(event)

    def _process_event(self, event: MBEvent) -> MBEvent | None:
        """Dedup, enrich and dispatch one notification event.

        Returns the dispatched event, or None when it was a duplicate, skipped
        as unusable, or suppressed. Isolating each event in its own call is what
        keeps one bad item from abandoning the rest of the batch.
        """
        data = event.data if isinstance(event.data, dict) else {}
        raw_notif_id = data.get("notification_id")
        notif_id = _notification_dedup_key(raw_notif_id)

        if notif_id is not None:
            dedup_key: int | str = notif_id
        elif not raw_notif_id:
            # No id at all. Skip-and-log would be safe but useless: the provider
            # re-fetches the same page-1 window every cycle, so the event would
            # come back and be re-POSTed forever. Dedup on content instead.
            dedup_key = _synthetic_notification_key(event)
            log.info(
                "Notification [%s] carries no notification_id — deduplicating on content key %s",
                _safe_log(event.event),
                dedup_key,
            )
            self._fix_event_id(event)
        else:
            # Present but not a number. Dispatching it would be guesswork, and
            # dropping it without a record would re-log the same warning every
            # cycle, so record it and move on.
            log.warning(
                "Dropping notification with unusable notification_id %r ([%s] %s) "
                "— suppressing it from future cycles",
                raw_notif_id,
                _safe_log(event.event),
                _safe_log(data.get("title")),
            )
            self.state_manager.mark_notification_processed(
                _synthetic_notification_key(event)
            )
            return None

        if self.state_manager.is_notification_processed(dedup_key):
            return None

        log.info("New notification received: [%s] %s", _safe_log(event.event), _safe_log(data.get("title")))

        # 2. Stealth task detail enrichment
        class_id = data.get("class_id")
        task_id = data.get("task_id")
        old_task = self.state_manager.get_task(str(task_id)) if task_id else None
        task_info = None

        if class_id and task_id:
            # Snapshot old state BEFORE updating so we can detect grade transitions
            task_info = self.stealth_crawler.fetch_task_details(class_id, task_id)
            if task_info:
                is_new = old_task is None
                cls_name = task_info.get("class_name") or data.get("class_name") or ""
                enriched_title = task_info.get("title")
                existing_task_title = data.get("task_title")

                # Guard: If enriched title matches class name, retain existing valid task title
                if enriched_title and cls_name and enriched_title.strip().lower() == cls_name.strip().lower():
                    if existing_task_title and existing_task_title.strip().lower() != cls_name.strip().lower():
                        task_info["title"] = existing_task_title
                    elif old_task and old_task.get("title") and old_task.get("title", "").strip().lower() != cls_name.strip().lower():
                        task_info["title"] = old_task["title"]

                self.state_manager.update_task(task_info)
                if is_new:
                    self._suppress_past_milestones(task_info)
                data["enriched_task"] = task_info
                if task_info.get("title") and (not cls_name or task_info["title"].strip().lower() != cls_name.strip().lower()):
                    data["task_title"] = task_info["title"]
                if task_info.get("class_name"):
                    data["class_name"] = task_info["class_name"]
                if task_info.get("due_date"):
                    data["due_date"] = task_info["due_date"]

                # Grade change / initial grade detection:
                # 1. If task is newly created and already graded, promote to task_graded
                #    so user sees Grade Posted instead of New Task banner.
                # 2. If task is updated and grade changed (or newly graded), promote to task_graded.
                if event.event in ("task_created", "new_task"):
                    if is_task_graded(task_info):
                        event.event = "task_graded"
                        data["grade_letter"] = task_info.get("grade_letter")
                        data["grade_score"] = task_info.get("grade_score")
                        log.info(
                            "Task %s was created with released grade: promoting to task_graded "
                            "(letter=%s score=%s)",
                            task_id,
                            task_info.get("grade_letter"),
                            task_info.get("grade_score"),
                        )
                elif event.event in ("task_updated", "updated_task"):
                    if old_task is not None:
                        old_grade_display = format_grade_display(old_task, standalone=True)
                        new_grade_display = format_grade_display(task_info, standalone=True)
                        if new_grade_display != old_grade_display and new_grade_display != "None":
                            event.event = "task_graded"
                            data["grade_letter"] = task_info.get("grade_letter")
                            data["grade_score"] = task_info.get("grade_score")
                            log.info(
                                "Grade change detected for task %s: promoting to task_graded "
                                "(letter=%s score=%s)",
                                task_id,
                                task_info.get("grade_letter"),
                                task_info.get("grade_score"),
                            )
                    elif is_task_graded(task_info):
                        event.event = "task_graded"
                        data["grade_letter"] = task_info.get("grade_letter")
                        data["grade_score"] = task_info.get("grade_score")
                        log.info(
                            "Initial grade detected for task %s on task_updated: promoting to task_graded "
                            "(letter=%s score=%s)",
                            task_id,
                            task_info.get("grade_letter"),
                            task_info.get("grade_score"),
                        )

                # If still task_updated and task is already submitted or graded, suppress notification!
                if event.event in ("task_updated", "updated_task") and (
                    is_task_submitted_or_graded(task_info)
                    or (old_task is not None and is_task_submitted_or_graded(old_task))
                ):
                    log.info(
                        "Suppressing task_updated notification for task %s: already submitted or graded",
                        task_id,
                    )
                    self.state_manager.mark_notification_processed(dedup_key)
                    return None
        elif event.event in ("task_created", "new_task"):
            if is_task_graded(data):
                event.event = "task_graded"
                log.info(
                    "Grade detected in event data for task %s: promoting to task_graded",
                    task_id or data.get("title"),
                )

        # Normalize event names to standard vocabulary
        if event.event == "new_task":
            event.event = "task_created"
        elif event.event == "updated_task":
            event.event = "task_updated"
        elif event.event in ("assignment_graded", "grade_posted"):
            event.event = "task_graded"

        # Standardize task events payload
        if event.event in ("task_created", "task_updated", "task_graded"):
            combined: dict[str, Any] = {}
            if old_task:
                # Private bookkeeping keys (`_cached_at`) are state-manager
                # internals and must not leak into a webhook payload.
                combined.update(
                    {k: v for k, v in old_task.items() if not str(k).startswith("_")}
                )
            if task_info:
                combined.update(task_info)
            combined.update(data)

            # Ensure the genuine task title takes precedence
            if task_info and task_info.get("title"):
                combined["title"] = task_info["title"]
            elif data.get("task_title"):
                combined["title"] = data["task_title"]

            c_id = combined.get("class_id") or class_id
            t_id = combined.get("task_id") or combined.get("id") or task_id
            if not combined.get("url") and c_id and t_id:
                combined["url"] = f"{self.client.base}/student/classes/{c_id}/core_tasks/{t_id}"

            event.data = standardize_task_payload(combined)
            event.data["task_title"] = event.data["title"]

        # Dispatch event to webhooks
        results = self.dispatcher.dispatch(event)

        # Only mark processed if delivery succeeded on at least one endpoint or no endpoints configured
        if not self.config.webhooks or any(r.get("success") for r in results):
            self.state_manager.mark_notification_processed(dedup_key)
        return event

    def run_check_cycle(self) -> dict[str, Any]:
        """Run a single check cycle: poll notifications, enrich tasks, evaluate deadlines, and dispatch."""
        new_notifications_count = 0
        reminders_dispatched_count = 0
        dispatched_events: list[MBEvent] = []
        poll_error: str | None = None

        # 1. Poll Provider for newly arrived events
        try:
            events = self.provider.poll_events()
            for event in events:
                try:
                    dispatched = self._process_event(event)
                except Exception as exc:
                    # One malformed event must not abandon the rest of the batch:
                    # the old blanket handler sat outside the loop, so a single
                    # bad item silenced every event behind it, every cycle.
                    log.warning(
                        "Skipping event after processing error: %s", exc, exc_info=True
                    )
                    continue
                if dispatched is None:
                    continue
                dispatched_events.append(dispatched)
                new_notifications_count += 1

                # Mark processed only when no endpoint still owes the event.
                # `any(r.get("success"))` was wrong: one success plus one
                # transiently-failed endpoint marked the notification handled,
                # so the endpoint that failed was silently abandoned — the
                # `retryable_results` primitive existed for exactly this and
                # nothing called it. A *permanently* failed endpoint is not
                # retryable, so it does not re-poll forever.
                if notif_id and (not self.config.webhooks or not retryable_results(results)):
                    self.state_manager.mark_notification_processed(int(notif_id))

        except Exception as exc:
            # Swallowed for the benefit of the long-running loop, which must keep
            # polling — but reported in-band so a one-shot caller (`daemon run
            # --once`) does not have to guess whether the cycle actually polled.
            poll_error = f"{type(exc).__name__}: {exc}"
            log.warning("Notification polling encountered error: %s", exc)

        # 3. Evaluate Deadline Countdown Reminders
        try:
            ddl_events = self.scheduler.evaluate_deadlines(auto_mark=False)
            for ddl_event in ddl_events:
                results = self.dispatcher.dispatch(ddl_event)
                dispatched_events.append(ddl_event)
                reminders_dispatched_count += 1

                t_id = ddl_event.data.get("task_id")
                threshold = ddl_event.data.get("reminder_threshold")
                if t_id and threshold and (not self.config.webhooks or not retryable_results(results)):
                    self.state_manager.mark_reminder_dispatched(t_id, threshold)
        except Exception as exc:
            deadline_error: str | None = f"{type(exc).__name__}: {exc}"
            log.warning("Deadline evaluation error: %s", exc)
        else:
            deadline_error = None

        self.state_manager.save()

        return {
            "new_notifications": new_notifications_count,
            "reminders_dispatched": reminders_dispatched_count,
            "total_dispatched": len(dispatched_events),
            "dispatched_events": dispatched_events,
            # In-band failure signals. A cycle that could not poll is not the
            # same as a cycle that polled and found nothing, and callers need to
            # be able to tell them apart without parsing the log.
            "poll_error": poll_error,
            "deadline_error": deadline_error,
        }

    def run_forever(self) -> None:
        """Run the main daemon loop continuously until interrupted."""
        self.start()

    def start(self) -> None:
        """Start the background daemon loop."""
        self._running = True

        def _handle_signal(sig, frame) -> None:
            log.info("Signal %s received — initiating graceful shutdown...", sig)
            self._running = False

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            pass

        # Publish our pid for the whole life of the loop. Without it an
        # installed daemon (the generated unit's ExecStart is `daemon run`)
        # cannot be found by `daemon status` or stopped by `daemon stop`.
        self._acquire_pid_file()

        try:
            log.info("ManageBac Notification Daemon started (provider=%s)", self.config.provider)
            self.provider.start()

            # Initial on-start lifecycle callback (triggers full upcoming task refresh by default)
            log.info("Executing daemon on-start callback...")
            try:
                self.on_start(self)
            except Exception as exc:
                log.warning("Daemon on-start callback encountered error: %s", exc)

            if self.config.active_windows:
                log.info(
                    "Active hours in force: %s (local time) — no polling outside them",
                    ", ".join(f"{w[0]}-{w[1]}" for w in self.config.active_windows),
                )

            while self._running:
                # Active-hours gate. Outside the window the daemon sleeps instead of
                # polling, which is what `--active-hours-start/--active-hours-end`
                # and daemon.json's `active_windows` promise. `--once` never reaches
                # this loop — one cycle always runs, whatever the clock says.
                if not self._in_active_window():
                    self._sleep_until_active_window()
                    continue

                # Re-crawl the upcoming view on a 12h cadence. The interval is
                # measured from the last *attempt*, so an empty or failed sync
                # costs one crawl instead of one per cycle.
                if self._full_sync_due(time.time()):
                    self.sync_upcoming_tasks()

                # Run check cycle
                res = self.run_check_cycle()
                if res["total_dispatched"] > 0:
                    log.info(
                        "Cycle completed: %d new notifications, %d DDL reminders dispatched",
                        res["new_notifications"],
                        res["reminders_dispatched"],
                    )

                # Sleep with jitter
                jitter = random.uniform(0, self.config.poll_jitter_seconds)
                sleep_duration = max(1.0, self.config.poll_interval_seconds + jitter)

                # Responsive sleep check
                deadline = time.time() + sleep_duration
                while self._running and time.time() < deadline:
                    time.sleep(min(1.0, deadline - time.time()))

            self.provider.stop()
            log.info("ManageBac Notification Daemon stopped.")
        finally:
            self._release_pid_file()
