"""Async stream interface for ManageBacDaemon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import TYPE_CHECKING, Any

from .events import DaemonConfig, MBEvent
from .service import DaemonService

if TYPE_CHECKING:
    from ..client import ManageBacClient

log = logging.getLogger(__name__)


class ManageBacDaemon:
    """Async-friendly wrapper around ManageBac event generation."""

    def __init__(
        self,
        client: ManageBacClient,
        poll_interval_seconds: float = 120.0,
        config: DaemonConfig | None = None,
        service: DaemonService | None = None,
    ):
        self.client = client
        self.interval = poll_interval_seconds
        self.config = config or DaemonConfig(poll_interval_seconds=int(poll_interval_seconds))
        self._service = service or DaemonService(
            client=self.client,
            config=self.config,
        )
        self._queue: asyncio.Queue[MBEvent] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

    def stop(self) -> None:
        """Signal the daemon to stop polling."""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    def _check_for_deltas(self) -> list[MBEvent]:
        """Synchronous poll cycle run in worker thread."""
        try:
            res = self._service.run_check_cycle()
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                return res.get("dispatched_events") or []
            return []
        except Exception as exc:
            log.warning("Daemon poll cycle error: %s", exc)
            return []

    async def _poll_worker(self) -> None:
        """Background worker that puts new events into the async queue."""
        try:
            while self._running:
                try:
                    events = await asyncio.to_thread(self._check_for_deltas)
                except Exception as exc:
                    log.warning("Daemon poll cycle error: %s", exc)
                    events = []

                for ev in events:
                    await self._queue.put(ev)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass

    async def stream(self) -> AsyncIterator[MBEvent]:
        """Asynchronous generator yielding MBEvents as they are discovered."""
        self._running = True
        worker = asyncio.create_task(self._poll_worker())
        self._worker_task = worker
        try:
            while self._running:
                event = await self._queue.get()
                yield event
                self._queue.task_done()
        finally:
            self._running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._worker_task = None
