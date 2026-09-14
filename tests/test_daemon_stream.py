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


def test_daemon_stop_unblocks_stream():
    """Verify daemon.stop() unblocks consumers waiting on empty queue and stops stream."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=10.0)
        daemon._check_for_deltas = MagicMock(return_value=[])

        events_received: list[MBEvent] = []

        async def consumer():
            async for ev in daemon.stream():
                events_received.append(ev)

        consumer_task = asyncio.create_task(consumer())
        # Let consumer start and block on queue.get()
        await asyncio.sleep(0.03)

        # Call stop() from outside
        daemon.stop()

        # consumer should finish cleanly without timeout
        await asyncio.wait_for(consumer_task, timeout=1.0)
        assert daemon._running is False
        assert len(events_received) == 0

    asyncio.run(_test())


def test_daemon_stream_concurrent_call_raises_runtime_error():
    """Verify attempting concurrent stream() calls on the same daemon raises RuntimeError."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=10.0)
        daemon._check_for_deltas = MagicMock(return_value=[])

        stream1 = daemon.stream()
        task1 = asyncio.create_task(stream1.__anext__())
        await asyncio.sleep(0.02)

        # Now stream is active, starting a second stream should raise RuntimeError
        stream2 = daemon.stream()
        with pytest.raises(RuntimeError, match="stream is already active"):
            await stream2.__anext__()

        # Clean up stream1
        daemon.stop()
        with pytest.raises(StopAsyncIteration):
            await task1
        await stream1.aclose()

    asyncio.run(_test())


def test_daemon_stream_multiple_event_loops():
    """Verify ManageBacDaemon instance can be streamed across separate asyncio event loops."""
    mock_client = MagicMock()
    daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)

    event1 = MBEvent(event="task_created", data={"task_id": 101, "title": "Loop 1 Task"})
    event2 = MBEvent(event="task_created", data={"task_id": 202, "title": "Loop 2 Task"})

    # Run in Event Loop 1
    async def run_loop_1():
        daemon._check_for_deltas = MagicMock(return_value=[event1])
        res = []
        async for ev in daemon.stream():
            res.append(ev)
            break
        return res

    res1 = asyncio.run(run_loop_1())
    assert len(res1) == 1
    assert res1[0].data["task_id"] == 101

    # Run in Event Loop 2 (fresh event loop created by asyncio.run)
    async def run_loop_2():
        daemon._check_for_deltas = MagicMock(return_value=[event2])
        res = []
        async for ev in daemon.stream():
            res.append(ev)
            break
        return res

    res2 = asyncio.run(run_loop_2())
    assert len(res2) == 1
    assert res2[0].data["task_id"] == 202


def test_daemon_stream_initial_sync():
    """Verify initial sync is triggered on first cycle if tasks_cache is empty."""

    async def _test():
        mock_client = MagicMock()
        daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.01)

        # Mock service with empty tasks_cache and a mock on_start
        mock_service = MagicMock()
        mock_service.state_manager.tasks_cache = {}
        daemon._service = mock_service

        event = MBEvent(event="task_created", data={"task_id": 1})
        daemon._check_for_deltas = MagicMock(return_value=[event])

        stream_iter = daemon.stream()
        async for _ in stream_iter:
            break
        await stream_iter.aclose()

        mock_service.on_start.assert_called_once_with(mock_service)

    asyncio.run(_test())
