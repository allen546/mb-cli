"""Main daemon service orchestrating provider polling, task enrichment, scheduling, and dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
import logging
import random
import signal
import time
from typing import Any

from ..client import ManageBacClient, parse_due_date
from .events import DaemonConfig, MBEvent
from .provider import AbstractNotificationProvider, MNNHubProvider
from .scheduler import DDLScheduler
from .state import DaemonStateManager
from .stealth import StealthTaskCrawler
from .webhook import WebhookDispatcher

log = logging.getLogger(__name__)


class DaemonService:
    """Core daemon service running the real-time notification loop."""

    def __init__(
        self,
        client: ManageBacClient,
        config: DaemonConfig | None = None,
        state_manager: DaemonStateManager | None = None,
        provider: AbstractNotificationProvider | None = None,
        auth_refresh_fn: Callable[[], bool] | None = None,
    ):
        self.client = client
        self.config = config or DaemonConfig()
        self.state_manager = state_manager or DaemonStateManager()
        self.auth_refresh_fn = auth_refresh_fn
        self.provider = provider or MNNHubProvider(
            self.client, auth_refresh_fn=self.auth_refresh_fn
        )
        self.stealth_crawler = StealthTaskCrawler(self.client, self.config.stealth)
        self.scheduler = DDLScheduler(
            self.state_manager,
            self.config.reminders,
            submission_checker=self._check_is_task_submitted,
        )
        self.dispatcher = WebhookDispatcher(
            webhooks=self.config.webhooks, verify_tls=self.config.verify_tls
        )
        self._running = False
        self._last_full_sync: float = 0.0

    def sync_upcoming_tasks(self) -> int:
        """Fetch all upcoming tasks and populate in-memory deadline state."""
        log.info("Performing upcoming tasks sync...")
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
            if is_new:
                self._suppress_past_milestones(t)
            synced_count += 1
        self._last_full_sync = time.time()
        self.state_manager.last_synced_at = datetime.now(timezone.utc).isoformat()
        self.state_manager.prune_old_tasks()
        self.state_manager.save()
        log.info("Synced %d upcoming tasks into scheduler", synced_count)
        return synced_count

    def _suppress_past_milestones(self, task: dict[str, Any]) -> None:
        """Suppress reminder milestones that were already in the past when task was first discovered."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        due_str = task.get("due_date")
        if not task_id or not due_str:
            return
        due_dt = parse_due_date(due_str)
        if not due_dt:
            return
        now = datetime.now(due_dt.tzinfo) if due_dt.tzinfo else datetime.now()
        minutes_left = (due_dt - now).total_seconds() / 60.0
        for th in self.scheduler.reminders:
            if minutes_left < th.threshold_minutes:
                self.state_manager.mark_reminder_dispatched(task_id, th.name)

    def _check_is_task_submitted(self, class_id: str, task_id: str) -> bool:
        """Targeted check: verify task page badge and submission status before firing an alarm."""
        task = self.state_manager.get_task(task_id)
        if task and task.get("status") == "submitted":
            return True
        if task and not task.get("has_submit_button", True):
            return False

        try:
            # 1. Check live task page via stealth crawler (finds 'Submitted' badge)
            details = self.stealth_crawler.fetch_task_details(class_id, task_id)
            if details and details.get("status") == "submitted":
                return True
        except Exception as e:
            log.debug("Live task page check error for task %s: %s", task_id, e)

        try:
            # 2. Check dropbox table as fallback
            submissions = self.client.get_submissions(class_id, task_id)
            if submissions and not submissions[0].get("error"):
                return True
        except Exception as e:
            log.debug("Targeted dropbox check error for task %s: %s", task_id, e)

        return False

    def run_check_cycle(self) -> dict[str, Any]:
        """Run a single check cycle: poll notifications, enrich tasks, evaluate deadlines, and dispatch."""
        new_notifications_count = 0
        reminders_dispatched_count = 0
        dispatched_events: list[MBEvent] = []

        # 1. Poll Provider for newly arrived events
        try:
            events = self.provider.poll_events()
            for event in events:
                notif_id = event.data.get("notification_id")
                if notif_id and self.state_manager.is_notification_processed(int(notif_id)):
                    continue

                log.info("New notification received: [%s] %s", event.event, event.data.get("title"))

                # 2. Stealth task detail enrichment
                class_id = event.data.get("class_id")
                task_id = event.data.get("task_id")
                if class_id and task_id:
                    task_info = self.stealth_crawler.fetch_task_details(class_id, task_id)
                    if task_info:
                        is_new = self.state_manager.get_task(task_id) is None
                        self.state_manager.update_task(task_info)
                        if is_new:
                            self._suppress_past_milestones(task_info)
                        event.data["enriched_task"] = task_info
                        if task_info.get("title"):
                            event.data["task_title"] = task_info["title"]
                        if task_info.get("class_name"):
                            event.data["class_name"] = task_info["class_name"]
                        if task_info.get("due_date"):
                            event.data["due_date"] = task_info["due_date"]

                # Dispatch event to webhooks
                results = self.dispatcher.dispatch(event)
                dispatched_events.append(event)
                new_notifications_count += 1

                # Only mark processed if delivery succeeded on at least one endpoint or no endpoints configured
                if notif_id and (not self.config.webhooks or any(r.get("success") for r in results)):
                    self.state_manager.mark_notification_processed(int(notif_id))

        except Exception as exc:
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
                if t_id and threshold and (not self.config.webhooks or any(r.get("success") for r in results)):
                    self.state_manager.mark_reminder_dispatched(t_id, threshold)
        except Exception as exc:
            log.warning("Deadline evaluation error: %s", exc)

        self.state_manager.save()

        return {
            "new_notifications": new_notifications_count,
            "reminders_dispatched": reminders_dispatched_count,
            "total_dispatched": len(dispatched_events),
        }

    def run_forever(self) -> None:
        """Run the main daemon loop continuously until interrupted."""
        self.start()

    def start(self) -> None:
        """Start the background daemon loop."""
        self._running = True

        def _handle_signal(sig, frame):
            log.info("Signal %s received — initiating graceful shutdown...", sig)
            self._running = False

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            pass

        log.info("ManageBac Notification Daemon started (provider=%s)", self.config.provider)
        self.provider.start()

        # Initial task synchronization only if cache is empty
        if not self.state_manager.tasks_cache:
            self.sync_upcoming_tasks()
        else:
            log.info(
                "Loaded %d active tasks from state cache — skipping initial full crawl",
                len(self.state_manager.tasks_cache),
            )
            self._last_full_sync = time.time()

        full_sync_interval_sec = self.config.full_sync_interval_minutes * 60

        while self._running:
            start_time = time.time()

            # Only fallback recrawl if cache became empty or long fallback interval (12h) elapsed
            if not self.state_manager.tasks_cache or (
                full_sync_interval_sec > 0
                and time.time() - self._last_full_sync >= max(43200, full_sync_interval_sec)
            ):
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
