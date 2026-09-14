# Modular Architecture: mb-cli Core & Downstream Consumer Separation

**Date**: 2026-09-14  
**Status**: Approved (Ready for Implementation)  
**Author**: Allen Sun & Antigravity  

---

## 1. Background & Motivation

`mb-cli` began as an unopinionated Python client library and CLI tool providing programmatic access to ManageBac (authenticating, crawling, and parsing tasks, grades, and submissions). 

Over time, it accumulated personal notification application concerns:
* A standalone Bark push webhook receiver (`bark_webhook_receiver.py`).
* Opinionated visual line budgets and alarm sound selectors (`alarm`, `chime`, priority levels).
* School-specific course alias mappings (`course_aliases.json`).
* Subjective heuristics around whether an offline task without a submit button warrants a 15-minute siren.

This coupling makes the codebase harder to maintain, limits reuse, and blocks clean expansion into upcoming projects like a **Web Dashboard** or **Todoist Sync**.

### Goal
Formally decouple `mb-cli` into a clean, unopinionated core library/daemon that acts as a reliable **ManageBac Event Producer**, extracting personal push notification rules and alerting heuristics into separate downstream consumer applications.

---

## 2. System Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                       REPO: mb-cli (Core Foundation)                         │
│                                                                             │
│  1. ManageBacClient: Session management, auth, crawling, submissions       │
│  2. Data Model: Task, Submission, Grade, MBEvent                            │
│  3. CLI: mb auth, mb tasks, mb view, mb grades, mb submit, mb daemon        │
│  4. MCP Server: Model Context Protocol tools for AI agents                  │
│  5. Event Engine (ManageBacDaemon):                                         │
│     - CLI Webhook Dispatcher: mb daemon run --webhook-url <url>             │
│     - Python Async Stream: async for event in daemon.stream()               │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Emits Standard Typed MBEvents
                                       │ (task_created, task_graded, etc.)
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
┌──────────────────────────┐ ┌──────────────────────────┐ ┌───────────────────┐
│ REPO: mb-notifier (Bark) │ │   REPO: mb-dashboard     │ │ REPO: mb-todoist  │
│                          │ │                          │ │                   │
│ - Course name aliases    │ │ - Local SQLite/Postgres  │ │ - Todoist REST API│
│ - Bark iOS push alerts   │ │ - FastAPI / Next.js UI   │ │ - Auto-create &   │
│ - Alarm sirens & sounds  │ │ - Grade tracking charts  │ │   close tasks     │
│ - Quiz vs reading filter │ │ - Timeline calendar      │ │ - Two-way sync    │
└──────────────────────────┘ └──────────────────────────┘ └───────────────────┘
```

---

## 3. Data Contract: Standard `MBEvent` Schema

`mb-cli` emits pure, unopinionated facts extracted directly from ManageBac. It contains no aliases, sound names, or subjective alert priorities.

### 3.1 Event Types
| Event Name | Trigger Condition |
| :--- | :--- |
| `task_created` | A newly posted task is discovered on ManageBac |
| `task_updated` | An existing task has changes to due date, title, or details |
| `task_graded` | A grade or evaluation is published/changed (score, letter, or `N/A`) |
| `submission_created` | A coursework file submission is detected in the dropbox |
| `file_uploaded` | A teacher uploaded a classroom file/resource |
| `announcement_created` | A new announcement was posted in a class |
| `deadline_approaching` | A tracked upcoming task crosses a configured time threshold |

### 3.2 Payload Schema
```json
{
  "event": "task_graded",
  "event_id": "evt_9a8b7c6d5e4f",
  "timestamp": "2026-09-14T07:30:00Z",
  "data": {
    "task_id": 27535638,
    "class_id": 11511739,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Vocab Quiz 2",
    "due_date": "2026-09-15 10:00:00",
    "due_iso": "2026-09-15T10:00:00+08:00",
    "has_submit_button": false,
    "category": "Quiz",
    "status": "not-submitted",
    "grade_letter": "N/A",
    "grade_score": null,
    "url": "https://beijing101.managebac.cn/student/classes/11511739/core_tasks/27535638"
  }
}
```

---

## 4. Python SDK: Async Event Stream

Downstream Python applications (such as FastAPI web dashboards, bot frameworks, or Todoist sync services) consume events via a native async generator.

### 4.1 Interface Design
The caller manages their own event loop (`asyncio.run(main())` or Uvicorn). `ManageBacDaemon.stream()` schedules the polling worker on the caller's running loop using `asyncio.create_task()`.

Because `client.py` uses synchronous `requests` and BeautifulSoup parsing, the polling worker executes network and parsing cycles via `asyncio.to_thread()` to ensure the caller's event loop remains completely unblocked.

```python
import asyncio
from typing import AsyncIterator
from mb_cli.client import ManageBacClient
from mb_cli.daemon.events import MBEvent

class ManageBacDaemon:
    def __init__(
        self,
        client: ManageBacClient,
        poll_interval_seconds: int = 120,
    ):
        self.client = client
        self.interval = poll_interval_seconds
        self._queue: asyncio.Queue[MBEvent] = asyncio.Queue()

    async def _poll_worker(self) -> None:
        """Background worker that pushes new events into the async queue."""
        while True:
            # Run blocking HTTP/scraping in thread pool to keep the loop free
            new_events = await asyncio.to_thread(self._check_for_deltas)
            for ev in new_events:
                await self._queue.put(ev)
            await asyncio.sleep(self.interval)

    async def stream(self) -> AsyncIterator[MBEvent]:
        """Asynchronous generator yielding MBEvents sequentially."""
        worker = asyncio.create_task(self._poll_worker())
        try:
            while True:
                event = await self._queue.get()
                yield event
                self._queue.task_done()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
```

### 4.2 Consumer Example (Todoist / Dashboard / Notifier)
```python
import asyncio
from mb_cli import ManageBacClient
from mb_cli.daemon import ManageBacDaemon

async def main():
    client = ManageBacClient.from_config()
    daemon = ManageBacDaemon(client, poll_interval_seconds=60)

    async for event in daemon.stream():
        if event.event == "task_graded":
            print(f"Grade posted: {event.data['title']} -> {event.data['grade_letter']}")
        elif event.event == "task_created":
            print(f"New task: {event.data['title']}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 5. Extraction & Codebase Clean-Up Plan

### 5.1 Files to Remove from `mb-cli`
The following files contain personal or opinionated alerting logic and will be extracted to the standalone `mb-notifier` project:
1. `bark_webhook_receiver.py` (Bark webhook server and push formatting)
2. `course_aliases.json` (Personal course name alias mappings)
3. `tests/test_bark_webhook.py` (Bark-specific formatting test suite)
4. `docs/deployments/2026-09-04-bark-webhook-receiver-deployment.md` (Operational deployment documentation for the Bark server)

### 5.2 Files to Refactor in `mb-cli`
1. `src/mb_cli/daemon/service.py`:
   - Clean up event generation to strictly match `MBEvent` specification.
   - Remove any Bark-specific payload assumptions.
2. `src/mb_cli/daemon/scheduler.py`:
   - Keep generic deadline calculation emitting `deadline_approaching`, but remove opinionated alert priorities or audio flags.
3. `src/mb_cli/daemon/__init__.py`:
   - Export `ManageBacDaemon` with `.stream()` async generator support alongside CLI entry points.
4. `src/mb_cli/__main__.py`:
   - Ensure `mb daemon run --webhook-url <url>` cleanly dispatches pure `MBEvent` payloads to any target.

---

## 6. Resolution: Offline Task Reminders (`has_submit_button == False`)

By moving alerting policy out of `mb-cli` and into the consumer application (`mb-notifier`):
* `mb-cli` simply reports facts: `has_submit_button: false`, `category: "Quiz"`.
* The downstream notifier app decides how to handle it:
  * For quiz/test categories: Sends a gentle reminder (`📅 测验预告`, sound=`chime`).
  * For reading/lesson plans: Silences notifications.
  * Avoids firing un-dismissable high-priority sirens for tasks that cannot be submitted on ManageBac.

---

## 7. Documentation & Developer Guides

Documentation is a core deliverable of this separation to ensure external developers and consumer applications (such as the Web Dashboard and Todoist Sync) can easily build on `mb-cli`.

### 7.1 `README.md` Refresh
* Update project description: clean, unopinionated ManageBac Python SDK, CLI, and real-time Event Engine.
* Add **Python SDK Quickstart**:
  - Basic usage (`ManageBacClient`).
  - Real-time event streaming (`ManageBacDaemon.stream()`).
* Add **CLI Quickstart**:
  - Task viewing, grading, and submissions.
  - Running the webhook daemon: `mb daemon run --webhook-url <url>`.
* Remove all mentions of Bark, sound files, or personal school aliases.

### 7.2 Event Stream Reference (`docs/events.md`)
* Complete catalog of event types (`task_created`, `task_updated`, `task_graded`, `submission_created`, `file_uploaded`, `announcement_created`, `deadline_approaching`).
* JSON schema definitions for all payloads.
* Downstream integration recipes:
  - Example 1: FastAPI webhook receiver.
  - Example 2: Async Python subscriber loop (Todoist sync pattern).

### 7.3 Downstream Notifier Guide
* Documentation on how the standalone `mb-notifier` connects to `mb-cli` (either as an imported library or via webhook receiver on the Raspberry Pi).

---

## 8. Verification Plan

1. **Unit & Integration Tests**:
   - Verify `ManageBacDaemon.stream()` yields events properly on a running `asyncio` event loop.
   - Verify `asyncio.to_thread` prevents event loop blockage during crawl cycles.
   - Verify cancellation and cleanup when consumer exits `async for` loop.
   - Verify existing CLI commands (`mb tasks`, `mb view`, `mb grades`, `mb submit`) remain 100% functional.
   - Run full test suite with `pytest`.
2. **Backwards Compatibility**:
   - Ensure `mb daemon run --webhook-url` continues to dispatch valid JSON payloads over HTTP.
3. **Documentation Verification**:
   - Verify code examples in `README.md` and `docs/events.md` are accurate, runnable, and syntactically valid.

