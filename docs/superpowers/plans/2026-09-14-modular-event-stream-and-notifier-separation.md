# Modular Architecture: mb-cli Event Stream & Downstream Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Formally separate personal Bark push notifications from mb-cli, implement a native Python async event stream (`ManageBacDaemon.stream()`), and document the clean SDK and event specification for downstream integrations (Web Dashboard, Todoist Sync).

**Architecture:** Decouple mb-cli into an unopinionated ManageBac data access and event engine. Standardize `MBEvent` payloads to report pure facts, extract personal Bark files into a standalone downstream package (`extras/mb-notifier`), implement `ManageBacDaemon.stream()` using `asyncio.create_task` and `asyncio.to_thread`, and write comprehensive developer documentation.

**Tech Stack:** Python 3.10+, `asyncio`, `pytest`, `pytest-asyncio`, `requests`, `BeautifulSoup4`.

---

## File Structure

```text
mb-crawler/
├── src/tahuti/
│   ├── __init__.py                     # Modify: Export ManageBacDaemon alongside ManageBacClient
│   ├── daemon/
│   │   ├── __init__.py                 # Modify: Export ManageBacDaemon
│   │   ├── events.py                   # Modify: Ensure MBEvent strictly conforms to standard schema
│   │   ├── scheduler.py                # Modify: Remove subjective alarm sound/priority logic
│   │   ├── service.py                  # Modify: Cleanse event generation, add stream support
│   │   └── stream.py                   # Create: ManageBacDaemon async stream wrapper
│   └── __main__.py                     # Check: CLI mb daemon run compatibility
├── extras/
│   └── mb-notifier/                    # Create: Staging directory for standalone Bark notifier
│       ├── bark_webhook_receiver.py    # Moved from root
│       ├── course_aliases.json         # Moved from root
│       ├── test_bark_webhook.py        # Moved from tests/
│       └── README.md                   # Setup & deployment instructions
├── docs/
│   ├── events.md                       # Create: Comprehensive event stream specification
│   ├── downstream-notifier-guide.md    # Create: Guide for running standalone Bark notifier
│   └── superpowers/
│       └── specs/2026-09-14-...md      # Existing approved design spec
├── README.md                           # Modify: Clean up project description, add SDK quickstart
└── tests/
    ├── test_daemon_stream.py           # Create: Tests for ManageBacDaemon.stream()
    └── test_daemon_events.py           # Create: Tests for standardized MBEvent schema
```

---

### Task 1: Extract Personal Bark Notifier to Standalone Staging Directory

**Files:**
- Create: `extras/mb-notifier/README.md`
- Move: `bark_webhook_receiver.py` -> `extras/mb-notifier/bark_webhook_receiver.py`
- Move: `course_aliases.json` -> `extras/mb-notifier/course_aliases.json`
- Move: `tests/test_bark_webhook.py` -> `extras/mb-notifier/test_bark_webhook.py`
- Move: `docs/deployments/2026-09-04-bark-webhook-receiver-deployment.md` -> `extras/mb-notifier/DEPLOYMENT.md`

- [ ] **Step 1: Create the staging directory structure**

```bash
mkdir -p extras/mb-notifier
```

- [ ] **Step 2: Move Bark receiver, personal aliases, tests, and deployment guide into `extras/mb-notifier/`**

```bash
git mv bark_webhook_receiver.py extras/mb-notifier/bark_webhook_receiver.py
git mv course_aliases.json extras/mb-notifier/course_aliases.json
git mv tests/test_bark_webhook.py extras/mb-notifier/test_bark_webhook.py
git mv docs/deployments/2026-09-04-bark-webhook-receiver-deployment.md extras/mb-notifier/DEPLOYMENT.md
```

- [ ] **Step 3: Create `extras/mb-notifier/README.md` documenting its standalone nature**

Write `extras/mb-notifier/README.md`:
```markdown
# ManageBac Bark Notifier (Downstream Consumer)

This is a standalone consumer application for `mb-cli`. It receives ManageBac events (either via HTTP webhook or direct Python SDK) and sends push notifications to iOS devices via Bark with custom course aliases and alarm sounds.

## Requirements
- Python 3.10+
- `requests`
- `mb-cli` (installed via pip or git)

## Usage with mb-cli Daemon
1. Start this receiver:
   ```bash
   python bark_webhook_receiver.py --port 42617 --host 127.0.0.1
   ```
2. Start mb daemon to dispatch events:
   ```bash
   mb daemon run --webhook-url http://127.0.0.1:42617/webhook
   ```
```

- [ ] **Step 4: Run existing test suite to verify no other tests depend on Bark files**

Run: `pytest tests/`
Expected: PASS (all tests pass without `test_bark_webhook.py`).

- [ ] **Step 5: Commit changes**

```bash
git add extras/ docs/ tests/
git commit -m "refactor: extract bark webhook receiver and personal config to extras/mb-notifier"
```

---

### Task 2: Standardize `MBEvent` and Cleanse Daemon Event Generation

**Files:**
- Modify: `src/tahuti/daemon/events.py`
- Modify: `src/tahuti/daemon/service.py`
- Create: `tests/test_daemon_events.py`

- [ ] **Step 1: Write tests for standard `MBEvent` factory and validation**

Create `tests/test_daemon_events.py`:
```python
"""Tests for standard MBEvent schema and factory methods."""

from tahuti.daemon.events import MBEvent


def test_standard_mbevent_serialization():
    event = MBEvent(
        event="task_graded",
        data={
            "task_id": 12345,
            "class_id": 67890,
            "class_name": "AP Physics 1",
            "title": "Lab Practical",
            "due_date": "2026-09-20 12:00:00",
            "due_iso": "2026-09-20T12:00:00+08:00",
            "has_submit_button": False,
            "category": "Quiz",
            "status": "not-submitted",
            "grade_letter": "A",
            "grade_score": "95 / 100",
            "url": "https://example.managebac.cn/student/classes/67890/core_tasks/12345",
        },
    )
    d = event.to_dict()
    assert d["event"] == "task_graded"
    assert d["version"] == "1.0"
    assert "timestamp" in d
    assert d["data"]["task_id"] == 12345
    assert d["data"]["has_submit_button"] is False
    assert d["data"]["grade_letter"] == "A"
    assert d["data"]["due_iso"] == "2026-09-20T12:00:00+08:00"
```

- [ ] **Step 2: Run test to verify it passes with current `MBEvent`**

Run: `pytest tests/test_daemon_events.py -v`
Expected: PASS

- [ ] **Step 3: Ensure `src/tahuti/daemon/service.py` populates standard event data**

Inspect `src/tahuti/daemon/service.py` around event dispatch and ensure all events (`task_created`, `task_updated`, `task_graded`, `deadline_approaching`) populate `task_id`, `class_id`, `class_name`, `title`, `due_date`, `due_iso`, `has_submit_button`, `category`, `status`, `grade_letter`, `grade_score`, and `url` without opinionated Bark formatting.

- [ ] **Step 4: Run tests to ensure no regressions**

Run: `pytest tests/test_daemon_events.py tests/test_daemon_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add src/tahuti/daemon/ tests/test_daemon_events.py
git commit -m "feat(daemon): standardize MBEvent payload schema"
```

---

### Task 3: Implement `ManageBacDaemon.stream()` Async Generator SDK

**Files:**
- Create: `src/tahuti/daemon/stream.py`
- Modify: `src/tahuti/daemon/__init__.py`
- Modify: `src/tahuti/__init__.py`
- Create: `tests/test_daemon_stream.py`

- [ ] **Step 1: Write tests for `ManageBacDaemon.stream()`**

Create `tests/test_daemon_stream.py`:
```python
"""Tests for ManageBacDaemon async stream interface."""

import asyncio
from unittest.mock import MagicMock
import pytest

from tahuti.daemon import ManageBacDaemon
from tahuti.daemon.events import MBEvent


@pytest.mark.asyncio
async def test_daemon_stream_yields_events_and_terminates():
    mock_client = MagicMock()
    daemon = ManageBacDaemon(mock_client, poll_interval_seconds=0.05)

    test_event = MBEvent(
        event="task_created",
        data={"task_id": 999, "title": "Math Assignment"},
    )

    # Mock _check_for_deltas to yield test_event once then empty
    cycles = 0
    def mock_poll():
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            return [test_event]
        return []

    daemon._check_for_deltas = mock_poll

    received = []
    async for event in daemon.stream():
        received.append(event)
        if len(received) >= 1:
            break

    assert len(received) == 1
    assert received[0].event == "task_created"
    assert received[0].data["task_id"] == 999
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_daemon_stream.py -v`
Expected: FAIL with `ImportError: cannot import name 'ManageBacDaemon' from 'tahuti.daemon'`

- [ ] **Step 3: Implement `src/tahuti/daemon/stream.py`**

Create `src/tahuti/daemon/stream.py`:
```python
"""Async stream interface for ManageBacDaemon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import TYPE_CHECKING, Any

from .events import DaemonConfig, MBEvent
from .service import DaemonService
from .state import DaemonStateManager

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

    def _check_for_deltas(self) -> list[MBEvent]:
        """Synchronous poll cycle run in worker thread."""
        try:
            res = self._service.run_check_cycle()
            return res.get("dispatched_events") or []
        except Exception as exc:
            log.warning("Daemon poll cycle error: %s", exc)
            return []

    async def _poll_worker(self) -> None:
        """Background worker that puts new events into the async queue."""
        while self._running:
            events = await asyncio.to_thread(self._check_for_deltas)
            for ev in events:
                await self._queue.put(ev)
            await asyncio.sleep(self.interval)

    async def stream(self) -> AsyncIterator[MBEvent]:
        """Asynchronous generator yielding MBEvents as they are discovered."""
        self._running = True
        worker = asyncio.create_task(self._poll_worker())
        try:
            while self._running:
                event = await self._queue.get()
                yield event
                self._queue.task_done()
        finally:
            self._running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
```

- [ ] **Step 4: Export `ManageBacDaemon` from `src/tahuti/daemon/__init__.py` and `src/tahuti/__init__.py`**

In `src/tahuti/daemon/__init__.py`:
Add:
```python
from .stream import ManageBacDaemon
```
And add `"ManageBacDaemon"` to `__all__`.

In `src/tahuti/__init__.py`:
Add:
```python
from .daemon.stream import ManageBacDaemon
```
And add `"ManageBacDaemon"` to `__all__`.

- [ ] **Step 5: Run tests to verify `test_daemon_stream.py` passes**

Run: `pytest tests/test_daemon_stream.py -v`
Expected: PASS

- [ ] **Step 6: Commit changes**

```bash
git add src/tahuti/ tests/test_daemon_stream.py
git commit -m "feat(daemon): implement ManageBacDaemon.stream() async event generator"
```

---

### Task 4: Write Event Stream & Integration Documentation (`docs/events.md`)

**Files:**
- Create: `docs/events.md`

- [ ] **Step 1: Write `docs/events.md`**

Create `docs/events.md` with:
- Purpose: Complete specification of all `MBEvent` payloads.
- Table of events (`task_created`, `task_updated`, `task_graded`, `submission_created`, `file_uploaded`, `announcement_created`, `deadline_approaching`).
- Exact JSON payload schemas.
- Downstream integration recipe 1: FastAPI Webhook Receiver.
- Downstream integration recipe 2: Python Async Subscriber Loop (`daemon.stream()`) for Todoist or custom bots.

- [ ] **Step 2: Verify markdown formatting and links**

Ensure file contains valid GitHub markdown with code examples.

- [ ] **Step 3: Commit `docs/events.md`**

```bash
git add docs/events.md
git commit -m "docs: add comprehensive event stream specification and integration guide"
```

---

### Task 5: Refresh `README.md` & Write Downstream Notifier Guide

**Files:**
- Modify: `README.md`
- Create: `docs/downstream-notifier-guide.md`

- [ ] **Step 1: Update `README.md`**

In `README.md`:
- Clarify `mb-cli` description as an unopinionated Python SDK, CLI, and real-time Event Engine.
- Add "Real-Time Event Streaming" section featuring `daemon.stream()` async generator.
- Add "Webhook Dispatcher" section with `mb daemon run --webhook-url`.
- Ensure all legacy Bark and personal course alias references are removed.

- [ ] **Step 2: Create `docs/downstream-notifier-guide.md`**

Document how to run the extracted `extras/mb-notifier` application alongside `mb-cli`:
- Setting up the standalone notifier.
- Running the webhook daemon.
- Configuring course aliases and Bark sound rules in the notifier.

- [ ] **Step 3: Commit documentation updates**

```bash
git add README.md docs/downstream-notifier-guide.md
git commit -m "docs: refresh README.md and add downstream notifier guide"
```

---

### Task 6: Final Verification & Test Suite Execution

**Files:**
- All touched files

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 2: Verify CLI entry points**

Run:
```bash
python -m tahuti --help
python -m tahuti daemon --help
```
Expected: Help outputs render cleanly without import errors or warnings.

- [ ] **Step 3: Final git status check and commit if any stray artifacts remain**

Run: `git status`
Expected: Clean working tree.
