# ManageBac Event Stream & Integration Specification

This document defines the event data contract, delivery channels, standard payload schemas, and downstream integration recipes for `mb-cli`.

---

## 1. Overview & Architecture

### 1.1 Unopinionated Event Producer

`mb-cli` acts as an **unopinionated ManageBac Event Producer**. Its sole responsibility is to interact with ManageBac—authenticating, polling for updates, parsing coursework details, detecting deltas, and evaluating upcoming deadlines—and emitting standardized, typed facts.

`mb-cli` intentionally contains:
- **No subjective alert heuristics**: It does not decide whether an offline task without a submission button warrants an urgent sound or silence.
- **No device-specific push formatting**: It does not budget for notification line limits (e.g. Bark 4-line constraints).
- **No user or school-specific aliases**: Course names are reported exactly as published on ManageBac.

Downstream consumer applications (such as iOS push notifiers, Web Dashboards, Todoist synchronization bots, or Telegram/Discord channels) subscribe to these events and apply their own presentation, categorization, and alerting rules.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       mb-cli (Core Event Producer)                          │
│                                                                             │
│  - Session Auth & Automated Token Refresh                                   │
│  - Real-Time MNN Notification Polling & HTML Crawling                       │
│  - Background Deadline Countdown Evaluator                                  │
│  - State Tracking & Delta Detection                                         │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Standard MBEvent Envelopes
                                       │ (task_created, task_graded, etc.)
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                     ▼
┌──────────────────────────────────────┐  ┌───────────────────────────────────┐
│ Channel A: HTTP Webhook Dispatcher   │  │ Channel B: Python Async SDK       │
│                                      │  │                                   │
│ CLI: mb daemon run --webhook-url ... │  │ Code: async for ev in             │
│ - POST JSON to HTTP endpoints        │  │           daemon.stream():        │
│ - HMAC-SHA256 signature verification │  │ - In-process asyncio event loop   │
│ - Exponential backoff retry          │  │ - Non-blocking worker thread pool │
└──────────────────┬───────────────────┘  └─────────────────┬─────────────────┘
                   │                                        │
                   ▼                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Downstream Consumers                             │
│                                                                             │
│  - mb-notifier (Bark iOS push alerts, course aliases, alarm sounds)         │
│  - mb-dashboard (FastAPI / Next.js web application, SQLite analytics)      │
│  - mb-todoist (Two-way task synchronization, priority adjustments)          │
│  - Custom Bots (Discord, Telegram, Slack, Lark / Feishu integrations)       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Event Delivery Channels

Downstream systems can consume ManageBac events via two primary channels:

#### Channel A: HTTP Webhooks
- **Best for**: Microservices, polyglot environments (Node.js, Go, Rust, Ruby), separate processes, or remote servers.
- **Execution**:
  ```bash
  mb daemon run --webhook-url http://127.0.0.1:8000/webhook --secret "your-hmac-secret"
  ```
  Or daemonized in the background:
  ```bash
  mb daemon start --webhook-url http://127.0.0.1:8000/webhook
  ```
- **HTTP Request Specification**:
  - Method: `POST`
  - Headers:
    - `Content-Type: application/json; charset=utf-8`
    - `User-Agent: mb-crawler-daemon/1.0`
    - `X-MB-Event: <event_type>` (e.g. `task_created`, `task_graded`)
    - `X-MB-Signature: sha256=<hex_hmac>` (present if `--secret` is configured)
  - Retries: Up to 3 attempts with exponential backoff (`1s`, `2s`, `4s`).

#### Channel B: Python Async SDK (`ManageBacDaemon.stream()`)
- **Best for**: Python-native applications, background worker tasks, FastAPI lifespan tasks, and CLI bots.
- **Execution**:

```python
import asyncio
from mb_cli import ManageBacClient, ManageBacDaemon

async def main():
    client = ManageBacClient.from_config()
    daemon = ManageBacDaemon(client, poll_interval_seconds=60)

    async for event in daemon.stream():
        print(f"Received {event.event}: {event.data.get('title')}")

if __name__ == "__main__":
    asyncio.run(main())
```
- **Concurrency**: Fully non-blocking. Synchronous HTTP network crawling and HTML parsing run in worker threads via `asyncio.to_thread()`, keeping your application's `asyncio` event loop completely responsive.

---

## 2. Standard Event Envelope & Field Specification

All events dispatched over webhooks or emitted by `ManageBacDaemon.stream()` adhere to a uniform JSON envelope (`MBEvent`).

### 2.1 Envelope Properties

| Field | Type | Description | Example |
| :--- | :--- | :--- | :--- |
| `event` | `string` | The event type identifier. | `"task_created"` |
| `event_id` | `string` | Unique, idempotent identifier for the event. | `"evt_9a8b7c6d5e4f"` |
| `timestamp` | `string` | ISO-8601 UTC timestamp of when the event was generated. | `"2026-09-14T07:30:00Z"` |
| `version` | `string` | Envelope schema version (currently `"1.0"`). | `"1.0"` |
| `data` | `object` | Event-specific payload containing factual data attributes. | `{ ... }` |

### 2.2 The 12 Standard Task Fields

All task-related events (`task_created`, `task_updated`, `task_graded`, `deadline_approaching`) populate a consistent 12-field schema inside `data`. Downstream consumers can rely on these fields without checking varying key names.

| Field | Type | Nullable | Description | Example |
| :--- | :--- | :---: | :--- | :--- |
| `task_id` | `integer` / `string` | No | ManageBac internal unique ID for the task. | `1000014` |
| `class_id` | `integer` / `string` | Yes | ManageBac internal class / course ID. | `1000010` |
| `class_name` | `string` | Yes | Full canonical class name as declared on ManageBac. | `"English Language Arts I (Hons) - Group 2"` |
| `title` | `string` | No | Cleaned task title (stripped of prefixes like `"New Task: "`). | `"Vocab Quiz 2"` |
| `due_date` | `string` | Yes | Human-readable due date string from ManageBac. | `"2026-09-15 10:00:00"` |
| `due_iso` | `string` | Yes | Normalized ISO-8601 string with school timezone offset. | `"2026-09-15T10:00:00+08:00"` |
| `has_submit_button` | `boolean` | No | `true` if an online coursework dropbox accepts files on ManageBac; `false` for offline quizzes, in-class discussions, or reading tasks. | `false` |
| `category` | `string` | Yes | Task category or primary label. | `"Quiz"` |
| `status` | `string` | Yes | Submission status: `"not-submitted"`, `"submitted"`, or `"graded"`. | `"not-submitted"` |
| `grade_letter` | `string` | Yes | Letter grade or evaluation status (e.g. `"A"`, `"7"`, `"N/A"`). | `"A"` |
| `grade_score` | `string` | Yes | Raw numerical or fraction score, or `null`. | `"95 / 100"` |
| `url` | `string` | Yes | Direct canonical URL to the task details page. | `"https://demo-school.managebac.cn/student/classes/1000010/core_tasks/1000014"` |

---

## 3. Event Catalog

### 3.1 `task_created`
Discovered when a teacher publishes a new task on ManageBac.

**Trigger Conditions:**
- A new task appears in the ManageBac notification feed or upcoming tasks schedule that has not been seen in the local state.
- If the newly created task already includes a released score, it is automatically promoted to `task_graded` instead.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "task_created",
  "event_id": "evt_d2e3f4a5b6c7",
  "timestamp": "2026-09-14T08:00:00Z",
  "data": {
    "task_id": 1000014,
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Vocab Quiz 2",
    "due_date": "2026-09-16 10:00:00",
    "due_iso": "2026-09-16T10:00:00+08:00",
    "has_submit_button": false,
    "category": "Quiz",
    "status": "not-submitted",
    "grade_letter": null,
    "grade_score": null,
    "url": "https://demo-school.managebac.cn/student/classes/1000010/core_tasks/1000014"
  }
}
```

---

### 3.2 `task_updated`
Discovered when a teacher modifies an existing task (e.g. rescheduled due date, changed instructions, or edited title).

**Trigger Conditions:**
- An update notification is received for an existing task.
- **Suppression Rule**: If the task has already been submitted or graded, `task_updated` notifications are automatically suppressed by the daemon to prevent unnecessary noise for past work.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "task_updated",
  "event_id": "evt_a1b2c3d4e5f6",
  "timestamp": "2026-09-14T09:15:00Z",
  "data": {
    "task_id": 1000014,
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Vocab Quiz 2 (Rescheduled)",
    "due_date": "2026-09-17 10:00:00",
    "due_iso": "2026-09-17T10:00:00+08:00",
    "has_submit_button": false,
    "category": "Quiz",
    "status": "not-submitted",
    "grade_letter": null,
    "grade_score": null,
    "url": "https://demo-school.managebac.cn/student/classes/1000010/core_tasks/1000014"
  }
}
```

---

### 3.3 `task_graded`
Discovered when a teacher enters, changes, or publishes grades/evaluations for a task.

**Trigger Conditions:**
- A notification indicates an assignment was graded.
- A score or letter grade transition is detected during polling (e.g. `grade_letter` changed from `None` to `"A"`, or points updated).
- A newly created assignment is discovered with a pre-released grade.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "task_graded",
  "event_id": "evt_8f7e6d5c4b3a",
  "timestamp": "2026-09-14T11:45:00Z",
  "data": {
    "task_id": 27419820,
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Unit 1 Critical Essay",
    "due_date": "2026-09-10 23:59:00",
    "due_iso": "2026-09-10T23:59:00+08:00",
    "has_submit_button": true,
    "category": "Summative",
    "status": "graded",
    "grade_letter": "A",
    "grade_score": "96 / 100",
    "url": "https://demo-school.managebac.cn/student/classes/1000010/core_tasks/27419820"
  }
}
```

---

### 3.4 `deadline_approaching`
Triggered by the countdown scheduler when an uncompleted, unsubmitted task approaches its deadline.

**Trigger Conditions:**
- Time remaining until `due_iso` crosses a configured milestone (default thresholds: `24h`, `6h`, `1h`, `15m`).
- Task status is not `"submitted"` and not `"graded"`.
- A live verification request checks the task dropbox before dispatching to confirm the student has not submitted since the last crawl.

**Additional Payload Fields:**
- `time_remaining_minutes` (`float`): Decimal minutes remaining until the deadline.
- `reminder_threshold` (`string`): The threshold that triggered this alert (`"24h"`, `"6h"`, `"1h"`, `"15m"`).

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "deadline_approaching",
  "event_id": "evt_1000014_reminder_1h",
  "timestamp": "2026-09-15T09:00:00Z",
  "data": {
    "task_id": 1000014,
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Vocab Quiz 2",
    "due_date": "2026-09-15 10:00:00",
    "due_iso": "2026-09-15T10:00:00+08:00",
    "has_submit_button": false,
    "category": "Quiz",
    "status": "not-submitted",
    "grade_letter": null,
    "grade_score": null,
    "url": "https://demo-school.managebac.cn/student/classes/1000010/core_tasks/1000014",
    "time_remaining_minutes": 59.8,
    "reminder_threshold": "1h"
  }
}
```

---

### 3.5 `submission_created`
Discovered when a coursework file or text submission is registered in the task dropbox.

**Trigger Conditions:**
- A student uploads a file via `mb submit` or through the ManageBac web interface, resulting in a new entry in the submission dropbox history.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "submission_created",
  "event_id": "evt_5c6d7e8f9a0b",
  "timestamp": "2026-09-14T14:20:00Z",
  "data": {
    "task_id": 27419820,
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Unit 1 Critical Essay",
    "submission_id": "sub_99214",
    "file_name": "Allen_Sun_Essay_Final.pdf",
    "file_size": 248102,
    "submitted_at_iso": "2026-09-14T14:19:55+08:00",
    "url": "https://demo-school.managebac.cn/student/classes/1000010/core_tasks/27419820"
  }
}
```

---

### 3.6 `file_uploaded`
Discovered when a teacher uploads course materials, slides, or resource files to a class.

**Trigger Conditions:**
- Notification received indicating new classroom resources or attachments were added.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "file_uploaded",
  "event_id": "notif_449102",
  "timestamp": "2026-09-14T03:10:00Z",
  "data": {
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Unit 2 Reading Packet",
    "file_name": "Unit2_Readings_and_Notes.pdf",
    "file_url": "https://demo-school.managebac.cn/student/classes/1000010/files/99812/download",
    "author_name": "Dr. Henderson",
    "created_at_iso": "2026-09-14T03:09:40+08:00"
  }
}
```

---

### 3.7 `announcement_created`
Discovered when a class bulletin, teacher message, or general announcement is posted.

**Trigger Conditions:**
- Notification received indicating a class message or bulletin post.

**Payload Schema:**
```json
{
  "version": "1.0",
  "event": "announcement_created",
  "event_id": "notif_449215",
  "timestamp": "2026-09-14T05:30:00Z",
  "data": {
    "class_id": 1000010,
    "class_name": "English Language Arts I (Hons) - Group 2",
    "title": "Reminder: Library Session Tomorrow",
    "body_preview": "Please meet directly in the 3rd floor library tomorrow morning at 08:30 for research orientation.",
    "author_name": "Dr. Henderson",
    "created_at_iso": "2026-09-14T05:29:10+08:00",
    "url": "https://demo-school.managebac.cn/student/classes/1000010/messages/55120"
  }
}
```

---

## 4. Integration Recipes

Below are two production-ready recipes demonstrating how to receive and process events.

### Recipe 1: FastAPI Webhook Receiver

This recipe implements an HTTP server that:
- Validates the incoming JSON payload against Pydantic models.
- Verifies the `X-MB-Signature` header using HMAC-SHA256 (if a webhook secret is set).
- Dispatches each event to its dedicated asynchronous handler function.

Save this file as `webhook_receiver.py`:

```python
"""Production-ready FastAPI Webhook Receiver for mb-cli events.

Install dependencies:
    pip install fastapi uvicorn pydantic

Run server:
    uvicorn webhook_receiver:app --host 0.0.0.0 --port 8000

Connect mb daemon:
    mb daemon run --webhook-url http://127.0.0.1:8000/webhook --secret "your-secret-key"
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webhook_receiver")

app = FastAPI(
    title="ManageBac Webhook Receiver",
    description="Consumes events emitted by mb-cli daemon over HTTP webhooks.",
    version="1.0.0",
)

# Set to match the --secret passed to mb daemon, or leave empty if unauthenticated
WEBHOOK_SECRET: Optional[str] = "your-secret-key"


class TaskPayload(BaseModel):
    """The 12 standard task fields emitted by mb-cli."""
    task_id: Optional[int | str] = None
    class_id: Optional[int | str] = None
    class_name: Optional[str] = None
    title: str = ""
    due_date: Optional[str] = None
    due_iso: Optional[str] = None
    has_submit_button: bool = False
    category: Optional[str] = None
    status: Optional[str] = None
    grade_letter: Optional[str] = None
    grade_score: Optional[str] = None
    url: Optional[str] = None

    # Extended attributes for deadline_approaching
    time_remaining_minutes: Optional[float] = None
    reminder_threshold: Optional[str] = None


class EventEnvelope(BaseModel):
    """Standard MBEvent envelope format."""
    version: str = "1.0"
    event: str
    event_id: str
    timestamp: str
    data: dict[str, Any]


def verify_hmac_signature(
    payload_bytes: bytes,
    signature_header: Optional[str],
    secret: str,
) -> bool:
    """Verify HMAC-SHA256 signature in constant time."""
    if not secret:
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    x_mb_event: Optional[str] = Header(None, alias="X-MB-Event"),
    x_mb_signature: Optional[str] = Header(None, alias="X-MB-Signature"),
):
    body = await request.body()

    # 1. Verify HMAC signature if secret configured
    if WEBHOOK_SECRET and not verify_hmac_signature(body, x_mb_signature, WEBHOOK_SECRET):
        logger.warning("Rejected webhook request: invalid HMAC signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # 2. Parse envelope
    try:
        envelope = EventEnvelope.model_validate_json(body)
    except Exception as exc:
        logger.error("Failed to parse event JSON: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Malformed event JSON: {exc}",
        )

    logger.info("Handling event: '%s' (ID: %s)", envelope.event, envelope.event_id)

    # 3. Route to event-specific handlers
    if envelope.event == "task_created":
        task = TaskPayload.model_validate(envelope.data)
        await handle_task_created(envelope.event_id, task)
    elif envelope.event == "task_updated":
        task = TaskPayload.model_validate(envelope.data)
        await handle_task_updated(envelope.event_id, task)
    elif envelope.event == "task_graded":
        task = TaskPayload.model_validate(envelope.data)
        await handle_task_graded(envelope.event_id, task)
    elif envelope.event == "deadline_approaching":
        task = TaskPayload.model_validate(envelope.data)
        await handle_deadline_approaching(envelope.event_id, task)
    elif envelope.event == "submission_created":
        await handle_submission_created(envelope.event_id, envelope.data)
    elif envelope.event == "file_uploaded":
        await handle_file_uploaded(envelope.event_id, envelope.data)
    elif envelope.event == "announcement_created":
        await handle_announcement_created(envelope.event_id, envelope.data)
    elif envelope.event == "test_ping":
        logger.info("Test ping confirmed: %s", envelope.data.get("message"))
    else:
        logger.info("Received unknown event type: %s", envelope.event)

    return {"status": "ok", "event_id": envelope.event_id}


async def handle_task_created(event_id: str, task: TaskPayload) -> None:
    logger.info("--> [New Task] '%s' in '%s' (Due: %s)", task.title, task.class_name, task.due_date)
    # Downstream action: insert into database, dispatch alerts, etc.


async def handle_task_updated(event_id: str, task: TaskPayload) -> None:
    logger.info("--> [Task Updated] '%s' (Due: %s, Status: %s)", task.title, task.due_date, task.status)


async def handle_task_graded(event_id: str, task: TaskPayload) -> None:
    logger.info(
        "--> [Grade Released] '%s' -> Letter: %s, Score: %s",
        task.title,
        task.grade_letter or "N/A",
        task.grade_score or "None",
    )


async def handle_deadline_approaching(event_id: str, task: TaskPayload) -> None:
    logger.warning(
        "--> [DDL Alert %s] '%s' in '%s' (%.1f minutes left)",
        task.reminder_threshold,
        task.title,
        task.class_name,
        task.time_remaining_minutes or 0.0,
    )


async def handle_submission_created(event_id: str, data: dict[str, Any]) -> None:
    logger.info("--> [Submission] File '%s' submitted for task %s", data.get("file_name"), data.get("task_id"))


async def handle_file_uploaded(event_id: str, data: dict[str, Any]) -> None:
    logger.info("--> [Class File] '%s' uploaded in '%s'", data.get("file_name"), data.get("class_name"))


async def handle_announcement_created(event_id: str, data: dict[str, Any]) -> None:
    logger.info("--> [Announcement] '%s' in '%s'", data.get("title"), data.get("class_name"))


@app.get("/health")
def health_check():
    return {"status": "healthy"}
```

---

### Recipe 2: Python Async Subscriber Loop (Todoist / Custom Bot Pattern)

This recipe demonstrates consuming events directly in Python without running an HTTP server. It uses `ManageBacDaemon.stream()` as an asynchronous iterator to synchronize assignments into task managers like Todoist.

Save this file as `subscriber_sync.py`:

```python
"""Python Async Event Subscriber Loop (Todoist / Custom Bot Pattern).

Directly subscribes to ManageBac events in-process without requiring HTTP webhooks.
Uses ManageBacDaemon.stream() to process events as an async iterator.

Install dependencies:
    pip install mb-cli

Run:
    python subscriber_sync.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from mb_cli import ManageBacClient, ManageBacDaemon
from mb_cli.daemon.events import MBEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("subscriber_sync")


class TodoistSyncService:
    """Example downstream consumer syncing ManageBac tasks to Todoist."""

    def __init__(self, api_token: str | None = None) -> None:
        self.api_token = api_token
        # In production: self.api = TodoistAPI(api_token)
        self._synced_tasks: dict[int | str, str] = {}

    async def on_task_created(self, event: MBEvent) -> None:
        data = event.data
        task_id = data.get("task_id")
        title = data.get("title", "Untitled Task")
        class_name = data.get("class_name", "General")
        due_iso = data.get("due_iso")
        url = data.get("url", "")
        category = data.get("category", "Assignment")
        has_submit = data.get("has_submit_button", False)

        task_content = f"[{class_name}] {title}"
        task_description = (
            f"ManageBac Link: {url}\n"
            f"Category: {category}\n"
            f"Online Dropbox: {'Yes' if has_submit else 'No (Offline/In-class)'}"
        )

        logger.info("[Todoist] Creating task: '%s' (Due: %s)", task_content, due_iso)
        # Production call:
        # todoist_task = await asyncio.to_thread(
        #     self.api.add_task,
        #     content=task_content,
        #     description=task_description,
        #     due_datetime=due_iso,
        #     priority=2,
        # )
        # if task_id:
        #     self._synced_tasks[task_id] = todoist_task.id

    async def on_task_updated(self, event: MBEvent) -> None:
        data = event.data
        task_id = data.get("task_id")
        title = data.get("title")
        due_iso = data.get("due_iso")

        logger.info("[Todoist] Updating task ID %s: title='%s', due=%s", task_id, title, due_iso)
        # Production call:
        # if task_id in self._synced_tasks:
        #     await asyncio.to_thread(
        #         self.api.update_task,
        #         self._synced_tasks[task_id],
        #         due_datetime=due_iso,
        #     )

    async def on_task_graded(self, event: MBEvent) -> None:
        data = event.data
        task_id = data.get("task_id")
        title = data.get("title")
        letter = data.get("grade_letter") or "N/A"
        score = data.get("grade_score") or ""

        logger.info("[Todoist] Task graded: '%s' -> %s (%s)", title, letter, score)
        # Production call:
        # if task_id in self._synced_tasks:
        #     tid = self._synced_tasks[task_id]
        #     await asyncio.to_thread(
        #         self.api.add_comment,
        #         task_id=tid,
        #         content=f"ManageBac Grade: {letter} {score}".strip(),
        #     )
        #     await asyncio.to_thread(self.api.close_task, task_id=tid)

    async def on_deadline_approaching(self, event: MBEvent) -> None:
        data = event.data
        task_id = data.get("task_id")
        threshold = data.get("reminder_threshold")
        mins_left = data.get("time_remaining_minutes")

        logger.warning(
            "[Alert] Deadline approaching (%s milestone, %.0f min left) for task %s",
            threshold,
            mins_left or 0.0,
            task_id,
        )
        # Escalate task priority in Todoist as deadline approaches:
        # if threshold in ("1h", "15m") and task_id in self._synced_tasks:
        #     await asyncio.to_thread(
        #         self.api.update_task,
        #         self._synced_tasks[task_id],
        #         priority=4,  # Urgent
        #     )


async def main() -> None:
    # 1. Initialize client using saved credentials (~/.config/managebac/credentials.json)
    client = ManageBacClient.from_config()
    logger.info("Connected to ManageBac for student: %s (%s)", client.student_name, client.subdomain)

    # 2. Instantiate daemon with 60-second polling interval
    daemon = ManageBacDaemon(client, poll_interval_seconds=60)
    service = TodoistSyncService()

    # 3. Handle termination signals gracefully
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _trigger_shutdown():
        logger.info("Termination signal received. Unblocking stream...")
        daemon.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown)
        except NotImplementedError:
            pass  # Fallback for platforms without add_signal_handler

    logger.info("Starting ManageBacDaemon async stream...")

    try:
        # 4. Stream events asynchronously
        async for event in daemon.stream():
            logger.info("Received event: '%s' (ID: %s)", event.event, event.event_id)

            if event.event == "task_created":
                await service.on_task_created(event)
            elif event.event == "task_updated":
                await service.on_task_updated(event)
            elif event.event == "task_graded":
                await service.on_task_graded(event)
            elif event.event == "deadline_approaching":
                await service.on_deadline_approaching(event)
            elif event.event == "submission_created":
                logger.info("Coursework submitted for task %s", event.data.get("task_id"))
            elif event.event == "file_uploaded":
                logger.info("File uploaded: %s", event.data.get("title"))
            elif event.event == "announcement_created":
                logger.info("Announcement posted: %s", event.data.get("title"))

            if stop_event.is_set():
                break
    except asyncio.CancelledError:
        logger.info("Async stream loop cancelled.")
    finally:
        daemon.stop()
        logger.info("ManageBac subscriber terminated cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

---

## 5. Best Practices for Downstream Developers

### 5.1 Idempotency & Deduplication
Every event envelope contains a unique `event_id`:
- For notifications polled from ManageBac, the ID is prefixed with `notif_` (e.g. `notif_449215`).
- For deadline reminders, the ID reflects the task and threshold (e.g. `evt_1000014_reminder_1h`).
- For generated task events, a unique hexadecimal prefix is provided (e.g. `evt_d2e3f4a5b6c7`).

Always store processed `event_id` keys in your database or cache (e.g. Redis) to ensure idempotent processing across network retries or daemon restarts.

### 5.2 Handling Offline Tasks (`has_submit_button: false`)
ManageBac contains both digital submission dropboxes and offline class events (such as paper quizzes, spoken presentations, or reading material).

`mb-cli` faithfully reports `has_submit_button: false` for offline assignments. Downstream applications should adapt their alert rules accordingly:
- **Quizzes / Exams**: Filter on `category in ("Quiz", "Test", "Exam")` to display study reminders without prompting for a file upload.
- **Reading / Lesson Plans**: Filter out low-priority reading assignments or silence countdown sirens.
- **Avoid persistent sirens**: For tasks where `has_submit_button == false`, do not fire un-dismissable alarms, because the student has no action on ManageBac to resolve them.

### 5.3 Course Name Aliasing & Normalization
ManageBac course names can be long and verbose (e.g. `"English Language Arts I (Hons) - Group 2"`). `mb-cli` intentionally preserves the raw course name.

Consumers can implement user-friendly display aliases via a local lookup table (e.g. `course_aliases.json`):

```json
{
  "English Language Arts I (Hons) - Group 2": "English",
  "AP Physics 1 (Gr 11)": "Physics"
}
```

```python
def resolve_display_name(raw_name: str, aliases: dict[str, str]) -> str:
    return aliases.get(raw_name.strip(), raw_name)
```

### 5.4 Timezones & Date Parsing
ManageBac timestamps are localized to the school's geographical location.
- Always use the `due_iso` field when available, as it includes the explicit UTC offset (e.g. `2026-09-15T10:00:00+08:00`).
- When parsing `due_iso` in Python, use standard `datetime.fromisoformat()` to preserve timezone awareness.
