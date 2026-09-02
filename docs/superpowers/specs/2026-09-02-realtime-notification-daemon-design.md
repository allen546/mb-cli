# Real-Time Notification Daemon & Webhook System Design

**Date:** 2026-09-02  
**Status:** Approved  
**Author:** Allen Sun & Antigravity  

---

## 1. Objective

Provide a robust, real-time background notification daemon and webhook dispatching engine for ManageBac students. The system alerts students immediately when new tasks, grade releases, or announcements appear, and provides proactive, submission-aware countdown reminders (e.g. 24h, 6h, 1h, 15m) before assignment deadlines (DDLs).

The architecture is built with a pluggable provider model to support both ManageBac's web-based MNN Hub REST API today and future mobile push notification bridges (iOS APNs / Android Push via MITM Stream proxy logs).

---

## 2. Architecture & Components

```
                               ┌──────────────────────────────────────────────┐
                               │       ManageBac Notification Network         │
                               │        (https://mnn-hub.prod.faria.*)        │
                               └──────────────────────┬───────────────────────┘
                                                      │
                                                      │ Heartbeat (30s) + Jitter
                                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   mb daemon Runtime                                         │
│                                                                                             │
│  ┌──────────────────────────────┐    Normalized Events   ┌───────────────────────────────┐  │
│  │ AbstractNotificationProvider │───────────────────────▶│     Unified Event Pipeline    │  │
│  │  - MNNHubProvider (Active)   │                        │   (MBEvent Envelope Standard) │  │
│  │  - MobilePushProvider (Plug) │                        └───────────────┬───────────────┘  │
│  └──────────────────────────────┘                                        │                  │
│                                                                          ▼                  │
│  ┌──────────────────────────────┐   Enriched Task Data   ┌───────────────────────────────┐  │
│  │    Stealth Task Crawler      │◀───────────────────────│   DDL & Countdown Scheduler   │  │
│  │ (Class Workspace ➔ Task DDL) │                        │ (T-24h, T-6h, T-1h, T-15m)    │  │
│  └──────────────────────────────┘                        └───────────────┬───────────────┘  │
│                                                                          │                  │
│                                                                          ▼                  │
│  ┌──────────────────────────────┐   Dispatched Record    ┌───────────────────────────────┐  │
│  │      State Persistence       │◀───────────────────────│       Webhook Dispatcher      │  │
│  │ (daemon_state.json / Cache)  │                        │     (Generic HTTP POST/JSON)  │  │
│  └──────────────────────────────┘                        └───────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 AbstractNotificationProvider Interface
Defines the standard protocol for fetching or listening to notifications:
```python
class AbstractNotificationProvider(abc.ABC):
    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def poll_events(self) -> list[MBEvent]: ...

    @abc.abstractmethod
    def refresh_auth(self) -> bool: ...
```

* **`MNNHubProvider`** *(Default / Active)*:
  * Manages MNN Hub REST requests (`GET /notifications/stats` and `GET /notifications`).
  * Handles auto-healing on HTTP 401/403 by re-scraping `/student/notifications` with `bypass_cache=True` and triggering `_relogin_from_creds` when needed.
* **`MobilePushProvider`** *(Future / Extensible)*:
  * Pluggable interface for ingesting mobile app API push notifications or local proxy/bridge streams captured via Stream app or device push tokens.

### 2.2 Stealth Task Crawler
When a task notification or full synchronization triggers, the crawler enforces realistic human navigation:
1. Visits the class workspace (`/student/classes/{class_id}/calendar` or `/student/classes/{class_id}/core_tasks`).
2. Follows internal references to the target task (`/student/classes/{class_id}/core_tasks/{task_id}`).
3. Extracts exact due datetime, instructions, attachments, and current submission status (`submitted` vs `not-submitted`).

### 2.3 Deadline Countdown & Reminder Scheduler
* Evaluates all upcoming tasks in memory every 60 seconds against configured thresholds (default: $24\text{h}, 6\text{h}, 1\text{h}, 15\text{m}$).
* **Submission Awareness**: If a task has `status == "submitted"`, all upcoming countdown reminders for that task are automatically suppressed.
* **State Deduplication**: Dispatched milestone keys (e.g. `task_1000099:ddl_1h`) are recorded in `daemon_state.json` so no reminder is sent twice, even if the daemon restarts.

### 2.4 Webhook Dispatcher
* Delivers standard JSON event envelopes via HTTP POST with exponential retry backoff.
* Supports custom headers and HMAC signature verification if a secret is provided.

---

## 3. Webhook Specification

### 3.1 Standard Event Envelope
```json
{
  "version": "1.0",
  "event": "deadline_approaching",
  "event_id": "evt_1000099_reminder_1h",
  "timestamp": "2026-09-02T17:40:00Z",
  "data": {
    "task_id": 1000099,
    "title": "NAME LIST",
    "class_name": "AP AP Physics 1 CLASS 2 BLUE 2026-2027 (Grade 10)",
    "class_id": 1000012,
    "due_date": "2026-09-10T09:10:00+08:00",
    "time_remaining_minutes": 60,
    "status": "not-submitted",
    "has_submit_button": true,
    "url": "https://demo-school.managebac.cn/student/classes/1000012/core_tasks/1000099",
    "teacher": {
      "name": "Hao (Hao) Li",
      "initials": "HL"
    }
  }
}
```

### 3.2 Event Types
| Event Name | Trigger Condition |
|---|---|
| `deadline_approaching` | Approaching DDL milestone reached ($T-24\text{h}, 6\text{h}, 1\text{h}, 15\text{m}$) for an unsubmitted task. |
| `task_created` | New assignment or core task published by teacher. |
| `task_updated` | Existing task deadline or details modified. |
| `task_submitted` | Student dropbox submission detected/confirmed. |
| `assignment_graded` | New grade or rubric assessment published. |
| `announcement_created` | New class announcement or message posted. |
| `daily_brief` | Daily morning summary of tasks due today. |
| `test_ping` | Verification ping dispatched by `mb daemon test-webhook`. |

---

## 4. Configuration & State Management

### 4.1 Configuration File (`~/.config/mb-crawler/config.json`)
```json
{
  "profiles": {
    "default": {
      "school": "demo-school",
      "domain": "managebac.cn",
      "email": "student@example.edu",
      "daemon": {
        "enabled": true,
        "provider": "mnn_hub",
        "poll_interval_seconds": 30,
        "poll_jitter_seconds": 5,
        "full_sync_interval_minutes": 15,
        "reminders": [
          { "threshold_minutes": 1440, "name": "24h" },
          { "threshold_minutes": 360,  "name": "6h"  },
          { "threshold_minutes": 60,   "name": "1h"  },
          { "threshold_minutes": 15,   "name": "15m" }
        ],
        "webhooks": [
          {
            "url": "https://your-webhook-endpoint.com/mb-events",
            "secret": null,
            "events": ["*"],
            "enabled": true
          }
        ],
        "stealth": {
          "enabled": true,
          "fetch_parent_context": true
        }
      }
    }
  },
  "version": 1,
  "active_profile": "default"
}
```

### 4.2 State File (`~/.config/mb-crawler/daemon_state.json`)
```json
{
  "last_synced_at": "2026-09-02T17:40:00Z",
  "processed_notification_ids": [244677168, 244677167],
  "dispatched_reminders": [
    "task_1000099:ddl_24h",
    "task_1000099:ddl_6h"
  ],
  "tasks_cache": {
    "1000099": {
      "title": "NAME LIST",
      "due_date": "2026-09-10T09:10:00+08:00",
      "status": "not-submitted",
      "class_id": 1000012
    }
  }
}
```

---

## 5. CLI Management Interface

| Command | Action |
|---|---|
| `mb daemon run` | Run in the foreground with live log output to stdout. |
| `mb daemon start` | Start background daemon process (records PID in `daemon.pid`). |
| `mb daemon stop` | Send `SIGTERM` to running background daemon and clean up PID. |
| `mb daemon status` | Display status (running/stopped, PID, uptime, active deadlines). |
| `mb daemon test-webhook` | Dispatch a mock `test_ping` event to all configured webhooks. |
| `mb daemon install` | Install auto-start service (macOS `launchd` plist / Linux `systemd --user`). |
| `mb daemon uninstall` | Remove and disable the auto-start service. |

---

## 6. Verification & Testing Plan

1. **Unit & Component Tests**:
   - `test_daemon_scheduler.py`: Test reminder triggers at $T-24\text{h}$, $T-1\text{h}$, etc., ensuring submission suppression works.
   - `test_webhook_dispatcher.py`: Mock HTTP server testing JSON payloads, retry backoff, and signature verification.
   - `test_state_persistence.py`: Verify state save/load and deduplication of reminder keys and notification IDs.
   - `test_mnn_provider.py`: Verify MNN Hub polling, delta detection, and 401 token auto-renewal.
2. **Live Integration Verification**:
   - Run `mb daemon test-webhook` against a local HTTP test server (e.g. `httpbin` / Python `http.server`).
   - Run `mb daemon run` with live `demo-school.managebac.cn` credentials to confirm heartbeat polling, task detail stealth fetching, and state recording.
