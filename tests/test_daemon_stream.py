"""Tests for ManageBacDaemon async stream interface."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock
import pytest

from mb_cli.daemon import ManageBacDaemon
from mb_cli.daemon.events import MBEvent


def test_daemon_stream_yields_events_and_terminates():
    """Verify ManageBacDaemon.stream() yields events as discovered and cancels cleanly on break."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)

        test_event = MBEvent(
            event="task_created",
            data={"task_id": 999, "title": "Math Assignment"},
        )

        cycles = 0

        def mock_poll():
            nonlocal cycles
            cycles += 1
            if cycles == 1:
                return [test_event]
            return []

        daemon._check_for_deltas = mock_poll

        received: list[MBEvent] = []
        stream_iter = daemon.stream()
        async for event in stream_iter:
            received.append(event)
            if len(received) >= 1:
                break
        await stream_iter.aclose()

        assert len(received) == 1
        assert received[0].event == "task_created"
        assert received[0].data["task_id"] == 999
        assert daemon._running is False

    asyncio.run(_test())


def test_daemon_stream_empty_cycles_and_multiple_events():
    """Verify empty cycles emit nothing and multiple events in a cycle yield in order."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)

        event1 = MBEvent(event="task_created", data={"task_id": 1, "title": "Task 1"})
        event2 = MBEvent(event="task_updated", data={"task_id": 2, "title": "Task 2"})
        event3 = MBEvent(event="deadline_approaching", data={"task_id": 1, "threshold": "1h"})

        cycle_responses = [
            [],                   # Cycle 1: empty
            [],                   # Cycle 2: empty
            [event1, event2],     # Cycle 3: 2 events
            [event3],             # Cycle 4: 1 event
        ]
        call_idx = 0

        def mock_poll():
            nonlocal call_idx
            if call_idx < len(cycle_responses):
                res = cycle_responses[call_idx]
                call_idx += 1
                return res
            return []

        daemon._check_for_deltas = mock_poll

        received: list[MBEvent] = []
        stream_iter = daemon.stream()
        async for event in stream_iter:
            received.append(event)
            if len(received) >= 3:
                break
        await stream_iter.aclose()

        assert len(received) == 3
        assert received[0].data["task_id"] == 1
        assert received[0].event == "task_created"
        assert received[1].data["task_id"] == 2
        assert received[1].event == "task_updated"
        assert received[2].data["task_id"] == 1
        assert received[2].event == "deadline_approaching"
        assert daemon._running is False

    asyncio.run(_test())


def test_daemon_stream_exception_handling_in_poll(caplog):
    """Verify exceptions in the poll worker are logged and do not crash the consumer generator."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)

        event_ok = MBEvent(event="task_created", data={"task_id": 42, "title": "History Paper"})
        attempt = 0

        def faulty_poll():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise ConnectionError("Network unreachable")
            return {"dispatched_events": [event_ok]}

        daemon._service = MagicMock()
        daemon._service.run_check_cycle.side_effect = faulty_poll

        received: list[MBEvent] = []
        stream_iter = daemon.stream()
        with caplog.at_level(logging.WARNING):
            async for event in stream_iter:
                received.append(event)
                if len(received) >= 1:
                    break
            await stream_iter.aclose()

        assert len(received) == 1
        assert received[0].data["task_id"] == 42
        assert daemon._running is False
        assert any("Daemon poll cycle error" in record.message or "Network unreachable" in record.message for record in caplog.records)

    asyncio.run(_test())


def test_daemon_stream_consumer_cancellation():
    """Verify cancelling consumer task cancels stream worker cleanly."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)
        daemon._check_for_deltas = MagicMock(return_value=[])

        async def consumer():
            async for _ in daemon.stream():
                pass

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert daemon._running is False

    asyncio.run(_test())
