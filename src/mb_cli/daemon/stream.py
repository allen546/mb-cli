"""Async stream interface for ManageBacDaemon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import TYPE_CHECKING

from .events import DaemonConfig, MBEvent
from .service import DaemonService

if TYPE_CHECKING:
    from ..client import ManageBacClient

log = logging.getLogger(__name__)

_STOP_SENTINEL = object()


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
        self._queue: asyncio.Queue[MBEvent | object] | None = None
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

    def stop(self) -> None:
        """Signal the daemon to stop polling and unblock waiting stream consumers."""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
        if self._queue is not None:
            self._queue.put_nowait(_STOP_SENTINEL)

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
            # Perform initial sync if tasks_cache is empty
            state_mgr = getattr(self._service, "state_manager", None)
            tasks_cache = getattr(state_mgr, "tasks_cache", None) if state_mgr else None
            if not tasks_cache:
                try:
                    on_start = getattr(self._service, "on_start", None)
                    if callable(on_start):
                        await asyncio.to_thread(on_start, self._service)
                    elif hasattr(self._service, "sync_upcoming_tasks"):
                        await asyncio.to_thread(self._service.sync_upcoming_tasks)
                except Exception as exc:
                    log.warning("Initial daemon sync error: %s", exc)

            while self._running:
                try:
                    events = await asyncio.to_thread(self._check_for_deltas)
                except Exception as exc:
                    log.warning("Daemon poll cycle error: %s", exc)
                    events = []

                if self._queue is not None:
                    for ev in events:
                        await self._queue.put(ev)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass

    async def stream(self) -> AsyncIterator[MBEvent]:
        """Asynchronous generator yielding MBEvents as they are discovered."""
        if self._running:
            raise RuntimeError("ManageBacDaemon stream is already active")

        self._running = True
        self._queue = asyncio.Queue()
        worker = asyncio.create_task(self._poll_worker())
        self._worker_task = worker
        try:
            while self._running:
                item = await self._queue.get()
                if item is _STOP_SENTINEL:
                    self._queue.task_done()
                    break
                yield item
                self._queue.task_done()
        finally:
            self._running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._worker_task = None
            self._queue = None
