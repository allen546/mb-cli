# ManageBac Downstream Notifier Guide (Bark iOS)

This guide documents the architecture, configuration, and operation of the standalone Bark push notification consumer (`extras/mb-notifier`) running alongside `mb-cli`.

---

## 1. Architectural Overview

`mb-cli` is architected as an **unopinionated event producer**. It handles authentication, ManageBac MNN notification polling, HTML delta crawling, and deadline countdown tracking. It dispatches standardized, typed event envelopes (`MBEvent`) over HTTP webhooks or through Python's `async for event in daemon.stream()`.

The standalone notifier in `extras/mb-notifier` acts as a **specialized downstream consumer**. It listens for webhook events from `mb-cli`, filters redundant alerts, maps course names to concise aliases, formats compact multi-line text optimized for iOS notification screens, and routes alerts to Apple devices via the Bark push notification service.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    ManageBac Cloud (managebac.com / .cn)                     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTPS (Polling & HTML Scrape)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       mb-cli (Core Event Producer)                          │
│                                                                             │
│  - Session Auth & Automated Token Refresh                                   │
│  - Real-Time MNN Notification Polling & HTML Crawling                       │
│  - Background Deadline Countdown Evaluator                                  │
│  - State Tracking & Delta Detection                                         │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTP POST /webhook (JSON MBEvent)
                                       │ (Headers: X-MB-Event, X-MB-Signature)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│             extras/mb-notifier (bark_webhook_receiver.py)                   │
│                                                                             │
│  - HTTP Webhook Server (Default port: 42617)                                │
│  - Event De-duplication & State-Based Suppression                           │
│  - Course Alias Translation (course_aliases.json, dynamic hot-reload)       │
│  - Mobile Viewport Optimization (Strict 3-field / 4-line layout)            │
│  - Event-Specific Sound & Priority Selection                                │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ CLI Execution / Local Subprocess
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Bark Push Client                               │
│                                                                             │
│  - Local CLI binary (/mnt/pi-data/tools/bark or custom path)                 │
│  - Apple Push Notification service (APNs) Delivery                          │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ APNs Push
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       End-User iOS & macOS Devices                          │
│                                                                             │
│  - iPhone / iPad / Apple Watch / Mac (Bark App)                             │
│  - Instant 1-click tap-through URL opening ManageBac task directly           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Notification Design & Alert Rules

### 2.1 Viewport Budget & Compact 3-Field Layout

Mobile lockscreens (especially iOS notification banners) truncate notifications after 3 to 4 lines of text. Long course names or teacher headers often push critical deadlines or task titles completely out of view.

`bark_webhook_receiver.py` enforces a **strict 3-field layout** guaranteed to fit within 4 visual lines:

- **Field 1 (`课程:`)**: Capped at 40 characters (strictly 1 visual line). Displays clean, user-configured course aliases without teacher noise.
- **Field 2 (`作业:` / `课件:` / `主题:` / `内容:`)**: Budgeted for up to 80 characters (allowing long task titles to wrap across up to 2 visual lines without cutting off).
- **Field 3 (`截止:` / `得分:` / `上传:` / `时间:`)**: Capped at 40 characters (1 visual line at the bottom). Guaranteed to remain visible regardless of title length.

Example notification banner on iOS:
```text
📝 New Task
课程: ELA Hons
作业: Reading Analysis Chapter 4 Discussion Questions
截止: 09-18 23:59 (今晚)
```

### 2.2 Teacher Information Omission

Teacher names (e.g. `George Lazo`, `Zhang Wei | 张伟`) are intentionally omitted from notification bodies. This conserves precious vertical line space for assignment requirements and deadlines.

### 2.3 Event Types, Sounds & Priority Matrix

Each ManageBac event is mapped to a dedicated alert sound, priority level, and title:

| Event Name | Alert Title | Bark Sound | Priority | Description |
| :--- | :--- | :--- | :--- | :--- |
| `task_created` | `📝 New Task` | `bell` | 6 | Newly published assignment or task |
| `task_updated` | `✏️ Updated Task` | `bell` | 5 | Changes to due date, task title, or description |
| `deadline_approaching` | `⏰ DDL Warning` | `alarm` | 10 | Imminent deadline reminder (e.g. 24h, 6h, 1h, 15m) |
| `task_graded` / `assignment_graded` | `📊 Grade Posted` | `chime` | 7 | Grades, points, or assessment letters released |
| `file_uploaded` | `📁 File Uploaded` | `bell` | 5 | New course file or resource uploaded by teacher |
| `announcement_created` | `📢 Class Announcement` | `bell` | 5 | Class notice or bulletin posted |
| `test_ping` | `🔔 Test Notification` | `bell` | 5 | Diagnostics / channel verification ping |

### 2.4 Handling Offline Tasks vs. Dropbox Tasks

ManageBac assignments fall into two operational categories:
1. **Dropbox Tasks**: Require digital file or text submissions on ManageBac.
2. **Offline Tasks**: In-class tests, oral presentations, handwritten quizzes, or physical submissions without an upload dropbox.

The receiver handles these differences intelligently:

- **Smart Suppression (`is_task_event_suppressed`)**:
  - If a task is already marked `submitted` or has a released grade, subsequent `task_updated` and `deadline_approaching` alerts are automatically suppressed. This eliminates panic-inducing countdown alerts for assignments that have already been handed in.
- **Offline Assessment Grading**:
  - When an offline quiz or exam is created with grades entered immediately by the teacher, the receiver identifies the released score (`has_released_grade`) and automatically promotes the event from `task_created` to `task_graded`, playing the `chime` sound and displaying the earned points instead of a blank submission deadline.
- **Deadline Formatting with Relative Context**:
  - Deadlines include contextual indicators: `今晚` (tonight), `明天` (tomorrow), `后天` (day after tomorrow), or `已超时` (overdue).
  - For approaching deadlines, countdown indicators (`仅剩 15分钟`, `还剩 1小时`) are included directly in Field 3.

---

## 3. Course Aliases (`course_aliases.json`)

Official course titles on ManageBac are frequently long, inconsistent, or laden with administrative identifiers (e.g. `AP English Language Arts I (Hons) - Group 2 (Grade 10)` or `AP AP—Calculus BC (Grade 10) Yellow`).

### 3.1 Mapping Format

`course_aliases.json` provides an exact, case-sensitive lookup mapping from official ManageBac course names to concise display labels:

```json
{
  "English Language Arts I (Hons) - Group 2": "ELA Hons",
  "English Language Arts I (Hons) - Group 2 (Grade 10)": "ELA Hons",
  "AP Calculus BC (Grade 10) Yellow": "AP Calc BC",
  "AP PRE-CALCULUS (Grade 10) Yellow": "Pre-Calc",
  "AP Physics 1 CLASS 2 BLUE 2026-2027 (Grade 10)": "AP Physics 1",
  "Accelerated Economics (AP Electives) E101": "AP Econ",
  "AP Pre-AP Biology Class20 (Grade 10) E101": "Pre-AP Bio",
  "AP Pre-AP Chemistry-2 (Grade 10) E101": "Pre-AP Chem"
}
```

### 3.2 Zero-Autocleaning Philosophy

`bark_webhook_receiver.py` does not perform heuristic regex stripping or fuzzy string guessing on course names. It strictly checks:
1. Exact match in `course_aliases.json`.
2. Fallback to the raw course name (truncated to 40 characters) if no alias is configured.

This ensures zero accidental mangling of unexpected course names.

### 3.3 Dynamic Hot-Reloading

The alias dictionary is cached in memory along with the file's modification time (`st_mtime`). 

When you add or edit a course alias in `course_aliases.json`:
- **No service restart is needed**.
- On the very next incoming webhook event, the receiver detects the updated file timestamp and reloads the configuration automatically.

---

## 4. Deployment & Operation

### 4.1 Prerequisites

- Python 3.10+
- `mb-cli` installed and authenticated
- Bark client (either the Bark CLI binary or a Bark server URL)
- Bark iOS app installed on target devices

### 4.2 Local Deployment (Development / Testing)

Run the receiver and the daemon locally in separate terminal tabs.

#### Step 1: Start the Webhook Receiver

```bash
python extras/mb-notifier/bark_webhook_receiver.py \
  --host 127.0.0.1 \
  --port 42617 \
  --bark-bin /path/to/bark \
  --course-aliases extras/mb-notifier/course_aliases.json
```

Output:
```text
2026-09-14 21:00:00 [INFO] Starting Bark Webhook Receiver on http://127.0.0.1:42617/webhook ...
```

#### Step 2: Verify Receiver Health

```bash
curl http://127.0.0.1:42617/health
```
Response:
```json
{"status":"ok","service":"bark_webhook_receiver"}
```

#### Step 3: Start the `mb-cli` Daemon

In a second terminal:

```bash
mb daemon run --webhook-url http://127.0.0.1:42617/webhook
```

To test the channel immediately, trigger a test ping:
```bash
mb daemon test-webhook http://127.0.0.1:42617/webhook
```

---

### 4.3 24/7 Remote Deployment (Raspberry Pi / Linux Server)

For continuous, reliable background notifications, run both `mb daemon` and `bark_webhook_receiver.py` on an always-on host (such as a Raspberry Pi or home server) using user-level `systemd` services.

#### Recommended File Locations

| File | Server Path |
| :--- | :--- |
| Virtualenv Python | `/opt/mb-tools/.venv/bin/python` |
| Webhook Receiver Script | `/opt/mb-notifier/bark_webhook_receiver.py` |
| Course Aliases | `~/.config/managebac/course_aliases.json` |
| Bark CLI Binary | `/opt/mb-tools/bark` |
| Systemd Service Units | `~/.config/systemd/user/` |

#### Step 1: Copy Files to Remote Server

```bash
# Create target directories
ssh user@server "mkdir -p /opt/mb-notifier ~/.config/managebac ~/.config/systemd/user"

# Copy receiver and aliases
scp extras/mb-notifier/bark_webhook_receiver.py user@server:/opt/mb-notifier/
scp extras/mb-notifier/course_aliases.json user@server:~/.config/managebac/course_aliases.json
```

#### Step 2: Create Systemd Unit for Webhook Receiver

Create `~/.config/systemd/user/mb-webhook-bark.service`:

```ini
[Unit]
Description=ManageBac Bark Webhook Receiver
After=network.target

[Service]
Type=simple
ExecStart=/opt/mb-tools/.venv/bin/python /opt/mb-notifier/bark_webhook_receiver.py \
    --host 127.0.0.1 \
    --port 42617 \
    --bark-bin /opt/mb-tools/bark \
    --course-aliases /home/user/.config/managebac/course_aliases.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

#### Step 3: Create Systemd Unit for Crawler Daemon

Create `~/.config/systemd/user/mb-daemon.service`:

```ini
[Unit]
Description=ManageBac Crawler Daemon
After=network.target mb-webhook-bark.service
Wants=mb-webhook-bark.service

[Service]
Type=simple
ExecStart=/opt/mb-tools/.venv/bin/mb daemon run \
    --webhook-url http://127.0.0.1:42617/webhook \
    --poll-interval 1800
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

#### Step 4: Enable User Lingering & Start Services

To ensure user systemd services continue running even after you disconnect from SSH:

```bash
# Enable linger for your user account
loginctl enable-linger $USER

# Reload systemd configuration
systemctl --user daemon-reload

# Enable and start services
systemctl --user enable --now mb-webhook-bark.service
systemctl --user enable --now mb-daemon.service
```

#### Step 5: Check Service Status & Logs

Check operational status:
```bash
systemctl --user status mb-webhook-bark.service mb-daemon.service --no-pager
```

Inspect real-time logs:
```bash
journalctl --user -u mb-webhook-bark.service -f
```

---

## 5. End-to-End Simulation & Verification

You can simulate live ManageBac events directly using `curl` against the webhook receiver:

### 5.1 Simulated New Task Event

```bash
curl -X POST http://127.0.0.1:42617/webhook \
  -H "Content-Type: application/json" \
  -H "X-MB-Event: task_created" \
  -d '{
    "event": "task_created",
    "timestamp": "2026-09-14T21:30:00Z",
    "data": {
      "class_name": "English Language Arts I (Hons) - Group 2",
      "task_title": "AP English Language Arts I (Hons) - Group 2 (Grade 10)",
      "due_date": "2026-09-18T23:59:00",
      "url": "https://school.managebac.cn/student/classes/11511739/core_tasks/27535638"
    }
  }'
```

Receiver Output Log:
```text
[INFO] Received event: task_created
[INFO] Dispatching to Bark:
       Title: '📝 New Task'
       Message:
       课程: ELA Hons
       作业: AP English Language Arts I (Hons) - Group 2 (Grade 10)
       截止: 09-18 23:59 (后天)
       Sound: 'bell', Priority: 6, URL: 'https://school.managebac.cn/student/classes/11511739/core_tasks/27535638'
[INFO] Bark pushed successfully: ✅ 已推送 (2 设备)
```

### 5.2 Simulated Approaching Deadline Warning

```bash
curl -X POST http://127.0.0.1:42617/webhook \
  -H "Content-Type: application/json" \
  -H "X-MB-Event: deadline_approaching" \
  -d '{
    "event": "deadline_approaching",
    "timestamp": "2026-09-14T21:30:00Z",
    "data": {
      "class_name": "AP Calculus BC (Grade 10) Yellow",
      "task_title": "Chapter 3 Problem Set: Derivatives",
      "due_date": "2026-09-14T22:30:00",
      "reminder_threshold": "1h",
      "time_remaining_minutes": 60,
      "url": "https://school.managebac.cn/student/classes/11511740/core_tasks/27535639"
    }
  }'
```

Receiver Output Log:
```text
[INFO] Received event: deadline_approaching
[INFO] Dispatching to Bark:
       Title: '⏰ DDL Warning'
       Message:
       课程: AP Calc BC
       作业: Chapter 3 Problem Set: Derivatives
       截止: 09-14 22:30 (还剩 1小时)
       Sound: 'alarm', Priority: 10, URL: 'https://school.managebac.cn/student/classes/11511740/core_tasks/27535639'
```

---

## 6. Troubleshooting

1. **Course Name Is Not Being Shortened**:
   - Check `course_aliases.json` for exact spelling and casing.
   - Verify that the path passed to `--course-aliases` exists and is valid JSON.
   - Look for warning messages in `journalctl --user -u mb-webhook-bark.service`.
2. **Push Notification Not Received**:
   - Test the Bark binary directly: `/path/to/bark -t "Test" "Hello from CLI"`.
   - Ensure the server has outbound internet access to APNs / Bark servers.
   - Check that the receiver is running and listening on the expected port: `curl http://127.0.0.1:42617/health`.
3. **Repeated Alerts for Completed Tasks**:
   - Ensure the task status on ManageBac is marked as `submitted` or has a grade score.
   - `is_task_event_suppressed` checks `status`, `labels`, and `grade_score`/`grade_letter` to filter out completed items automatically.
