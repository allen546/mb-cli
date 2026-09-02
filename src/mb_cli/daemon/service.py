"""Main daemon service orchestrating provider polling, task enrichment, scheduling, and dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import random
import signal
import time
from typing import Any

from ..client import ManageBacClient
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
    ):
        self.client = client
        self.config = config or DaemonConfig()
        self.state_manager = state_manager or DaemonStateManager()
        self.provider = provider or MNNHubProvider(self.client)
        self.stealth_crawler = StealthTaskCrawler(self.client, self.config.stealth)
        self.scheduler = DDLScheduler(self.state_manager, self.config.reminders)
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
            synced_count = 0
            for t in upcoming_tasks:
                task_id = t.get("id")
                if not task_id:
                    continue
                self.state_manager.update_task(t)
                synced_count += 1
            self._last_full_sync = time.time()
            self.state_manager.last_synced_at = datetime.now(timezone.utc).isoformat()
            self.state_manager.save()
            log.info("Synced %d upcoming tasks into scheduler", synced_count)
            return synced_count
        except Exception as exc:
            log.warning("Task sync error: %s", exc)
            return 0

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
                        self.state_manager.update_task(task_info)
                        event.data["enriched_task"] = task_info

                # Dispatch event to webhooks
                self.dispatcher.dispatch(event)
                dispatched_events.append(event)
                new_notifications_count += 1

                if notif_id:
                    self.state_manager.mark_notification_processed(int(notif_id))

        except Exception as exc:
            log.warning("Notification polling encountered error: %s", exc)

        # 3. Evaluate Deadline Countdown Reminders
        try:
            ddl_events = self.scheduler.evaluate_deadlines()
            for ddl_event in ddl_events:
                self.dispatcher.dispatch(ddl_event)
                dispatched_events.append(ddl_event)
                reminders_dispatched_count += 1
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
        self._running = True

        def _handle_signal(signum, frame):
            log.info("Signal %d received — shutting down daemon cleanly...", signum)
            self._running = False

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            pass

        log.info("ManageBac Notification Daemon started (provider=%s)", self.config.provider)
        self.provider.start()

        # Initial task synchronization
        self.sync_upcoming_tasks()

        full_sync_interval_sec = self.config.full_sync_interval_minutes * 60

        while self._running:
            start_time = time.time()

            # Periodic full sync if interval elapsed
            if time.time() - self._last_full_sync >= full_sync_interval_sec:
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
