# Deployment Log & Operational Notes

**Date**: 2026-09-04  
**Target Host**: `100.95.33.78` (Raspberry Pi 4 / Debian aarch64, Tailscale network)  
**Service**: ManageBac Bark Webhook Receiver & Crawler Daemon  

---

## 1. Summary of Changes Deployed
- **Compact 3-Field Notification Layout**:
  - Allocated strictly at most 4 visual lines across 3 logical fields (`课程:`, `作业:`, `截止:`/`得分:`).
  - Field 1 (`课程:`) capped at 40 characters (1 visual line).
  - Field 2 (`作业:`) allocated up to 80 characters (allowing long titles to wrap across 2 visual lines).
  - Field 3 (`截止:`/`得分:`) capped at 40 characters (1 visual line at the bottom).
  - Guaranteed deadline is never pushed off screen.
- **Teacher Information Removed**: Teacher names omitted completely from all notification bodies.
- **English Alert Titles**: Clean titles without markdown formatting (`📝 New Task`, `⏰ DDL Warning`, `✏️ Updated Task`, `📊 Grade Posted`, `📁 File Uploaded`, `📢 Class Announcement`, `🔔 Test Notification`).
- **Exact Course Alias Resolution (Zero Autocleaning)**:
  - Course names are matched exact, case-sensitive against user aliases without heuristic regex or destructive autocleaning.
  - Configured in `~/.config/managebac/course_aliases.json` with mtime-based hot reloading.
  - Active profile is Beijing 101 (`beijing101.managebac.cn`).

---

## 2. File Manifest & Paths

| File | Local Path | Remote Path (`100.95.33.78`) |
| :--- | :--- | :--- |
| Webhook Receiver Script | `bark_webhook_receiver.py` | `/mnt/pi-data/mb-crawler/bark_webhook_receiver.py` |
| Course Aliases Config | `course_aliases.json` | `/mnt/pi-data/mb-crawler/course_aliases.json`<br>`/home/allen/.config/managebac/course_aliases.json` |
| Local User Aliases | `~/.config/managebac/course_aliases.json` | - |
| Unit Test Suite | `tests/test_bark_webhook.py` | - |

---

## 3. Deployment Sequence

### Step 1: File Sync
```bash
# Ensure local config directory exists
mkdir -p ~/.config/managebac
cp course_aliases.json ~/.config/managebac/course_aliases.json

# Deploy receiver and aliases to Raspberry Pi
scp bark_webhook_receiver.py course_aliases.json 100.95.33.78:/mnt/pi-data/mb-crawler/
scp course_aliases.json 100.95.33.78:/home/allen/.config/managebac/course_aliases.json
```

### Step 2: Service Restart & Verification
Both services run under the `allen` user systemd instance:

```bash
# Restart webhook receiver
ssh 100.95.33.78 "systemctl --user restart mb-webhook-bark"

# Restart crawler daemon
ssh 100.95.33.78 "systemctl --user restart mb-daemon"

# Check statuses
ssh 100.95.33.78 "systemctl --user status mb-webhook-bark mb-daemon --no-pager"
```

**Status Confirmation**:
- `mb-webhook-bark.service`: Active (running), Main PID: 58569 (`/usr/bin/python3 /mnt/pi-data/mb-crawler/bark_webhook_receiver.py --port 42617 --host 127.0.0.1`)
- `mb-daemon.service`: Active (running), Main PID: 58584 (`/mnt/pi-data/tools/.venv/bin/mb daemon run --webhook-url http://127.0.0.1:42617/webhook`)

### Step 3: End-to-End Live Webhook Test
Simulated a live ManageBac `task_created` webhook on `127.0.0.1:42617` on the Raspberry Pi:

```bash
curl -X POST http://127.0.0.1:42617/webhook \
  -H "Content-Type: application/json" \
  -H "X-MB-Event: task_created" \
  -d '{
    "event": "task_created",
    "data": {
      "class_name": "English Language Arts I (Hons) - Group 2",
      "task_title": "AP English Language Arts I (Hons) - Group 2 (Grade 10)",
      "due_date": "2026-09-10T10:00:00",
      "sender": {"name": "George Lazo"},
      "url": "https://beijing101.managebac.cn/student/classes/11511739/core_tasks/27535638"
    }
  }'
```

**Execution Log (`systemctl --user status mb-webhook-bark`)**:
```text
Sep 04 14:13:01 raspberrypi python3[58569]: Received event: task_created
Sep 04 14:13:01 raspberrypi python3[58569]: Dispatching to Bark:
Sep 04 14:13:01 raspberrypi python3[58569]: Title: '📝 New Task'
Sep 04 14:13:01 raspberrypi python3[58569]: Message:
Sep 04 14:13:01 raspberrypi python3[58569]: 课程: ELA Hons
Sep 04 14:13:01 raspberrypi python3[58569]: 作业: AP English Language Arts I (Hons) - Group 2 (Grade 10)
Sep 04 14:13:01 raspberrypi python3[58569]: 截止: 09-10 10:00
Sep 04 14:13:01 raspberrypi python3[58569]: Sound: 'bell', Priority: 6, URL: 'https://beijing101.managebac.cn/student/classes/11511739/core_tasks/27535638'
Sep 04 14:13:03 raspberrypi python3[58569]: Bark pushed successfully: ✅ 已推送 (2 设备)
```

---

## 4. Operational Notes & Troubleshooting

1. **Course Name Aliases Updates**:
   - Location: `~/.config/managebac/course_aliases.json` on the Pi (or `--course-aliases <path>`).
   - Resolution is **exact and case-sensitive**.
   - Because `bark_webhook_receiver.py` checks file `st_mtime`, any edits to `course_aliases.json` take effect on the very next incoming notification **without restarting the service**.
2. **Checking Service Logs on the Pi**:
   - Since Debian user systemd sessions may not persist journal files across SSH sessions, use:
     ```bash
     ssh 100.95.33.78 "systemctl --user status mb-webhook-bark --lines 50 --no-pager"
     ```
3. **Daemon Webhook Connection**:
   - The daemon connects to the local receiver at `http://127.0.0.1:42617/webhook`.
   - Health check endpoint: `curl http://127.0.0.1:42617/health` returns `{"status":"ok","service":"bark_webhook_receiver"}`.
