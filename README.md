# mb-cli: ManageBac CLI, Python SDK & Real-Time Event Engine

An unopinionated, robust toolkit for **ManageBac**: typed Python SDK, command-line interface, Model Context Protocol (MCP) server for AI assistants, and a real-time event streaming and webhook engine.

Supports both international (`managebac.com`) and China (`managebac.cn`) instances.

---

## Core Capabilities

1. **Unopinionated Python SDK** (`ManageBacClient`, `ManageBacDaemon`):
   - Authenticate seamlessly via credentials or saved session cookies (`ManageBacClient.from_config()`).
   - Programmatic access to tasks, submissions, grades, calendar feeds, weekly timetables, and MNN notifications.
   - Clean separation of concerns: produces pure, typed data with zero vendor-specific assumptions or hardcoded push rules.
2. **Interactive CLI** (`mb login`, `mb list`, `mb view`, `mb grades`, `mb submit`, `mb daemon`):
   - Fast terminal workflows for everyday student tasks: listing assignments, viewing details, uploading files, inspecting grades, checking schedules, and managing background daemons.
   - Smart output formatting: human-friendly colored tables on interactive TTYs, structured JSON when piped to files or other tools (`jq`).
3. **MCP Server for AI Coding Assistants**:
   - Built-in Model Context Protocol server (`mb-mcp`) with 14 tools for AI assistants like Claude Desktop, Gemini, and Cursor to inspect deadlines, grades, and coursework.
4. **Real-Time Event Streaming & Webhook Engine**:
   - In-process async event streaming (`async for event in daemon.stream()`) for Python bots and background tasks.
   - Background daemon service (`mb daemon run --webhook-url ...`) dispatching typed `MBEvent` payloads to HTTP webhooks with HMAC-SHA256 signatures, exponential backoff retries, stealth jitter, and active-hours scheduling.
   - For the full event contract and JSON schema, see [Event Stream Specification](docs/events.md). For operational push notification setups (such as Bark for iOS), see [Downstream Notifier Guide](docs/downstream-notifier-guide.md).

---

## Disclaimer

**Use at your own risk.** This tool is an unofficial, community-maintained client and scraper. It is not affiliated with or endorsed by Faria Education Group or ManageBac. By using this tool, you acknowledge and accept the following:

Faria/ManageBac's legal documents restrict automated access:
- **robots.txt** (managebac.com): Disallows `/login`, `/admin`, `/api` for all user agents.
- **Terms of Use §1.2.6**: "Accounts registered by 'bots' or screen scrapers and/or other automated means are not permitted and access will be terminated without notice."
- **Terms of Service §5.5**: "Misuse of the Service, including but not limited to reverse engineering... may result in permanent and/or temporary suspension or termination of the School's account."
- **Terms of Service §1.4**: Violations may result in account termination without notice.
- **Terms of Service §9.4**: Schools exceeding 200 GB/month bandwidth may face caps or additional invoices.

**The authors bear no responsibility for any consequences resulting from its use, including account suspension or school-level penalties.** You are solely responsible for ensuring your use complies with your school's policies and ManageBac's Terms of Service.

---

## Installation

```bash
pip install .
```

The `mb-mcp` MCP server needs one extra dependency. `mcp` is *not* a runtime
dependency of the `mb` CLI, so a plain `pip install .` leaves `mb-mcp` failing
with `ModuleNotFoundError: No module named 'mcp'`:

```bash
pip install "mb-cli[mcp]"
```

Or install in editable mode for local development:
```bash
pip install -e .
pip install -e ".[mcp]"
```

This is also a [`uv`](https://docs.astral.sh/uv/) project, with the test
dependencies kept out of the published runtime environment in a `dev`
dependency group:

```bash
uv sync --group dev     # pytest + requests-mock + the mcp extra
uv run pytest
```

---

## Python SDK Quickstarts

> 📖 **Full Library Reference**: For complete method signatures, parameter types, status enums, MNN Hub integration, and production recipes, see [docs/library.md](docs/library.md).

### 1. Basic Client Usage (`ManageBacClient`)

Use `ManageBacClient` for synchronous fetching and actions:

```python
from mb_cli import ManageBacClient

# Option A: Authenticate automatically from saved local CLI credentials
client = ManageBacClient.from_config()

# Option B: Explicit authentication
# client = ManageBacClient(school="your-school", domain="managebac.com")
# client.login("student@example.com", "your-password")

# 1. Fetch upcoming tasks and coursework
tasks_data = client.crawl_all(fetch_details=True)
for task in tasks_data.get("upcoming", []):
    print(f"[{task.get('due_date')}] {task.get('title')} ({task.get('class_name')})")

# 2. View one task in detail
task_detail = client.get_task_detail("/student/classes/1000024/core_tasks/1000025")
if task_detail:
    print(task_detail.get("description"))

# 3. Check class grades and computed expected scores
grades = client.get_class_grades(class_id="1000023")
print(f"Expected Grade: {grades.get('expected_grade')}")

# 4. View calendar events
events = client.get_calendar_events(start="2026-09-01", end="2026-09-07")

# 5. Fetch weekly timetable
timetable = client.get_timetable()

# 6. Upload homework file to assignment dropbox
client.submit_file(
    class_id="1000023",
    task_id="1000026",
    file_path="homework.pdf",
)
```

### 2. Real-Time Async Event Streaming (`ManageBacDaemon`)

Use `ManageBacDaemon.stream()` to consume live ManageBac events asynchronously in your Python application:

```python
import asyncio
from mb_cli import ManageBacClient, ManageBacDaemon

async def main():
    # Load authenticated client
    client = ManageBacClient.from_config()

    # Create daemon instance (crawling runs in worker threads, non-blocking)
    daemon = ManageBacDaemon(client, poll_interval_seconds=60)

    print("Subscribed to ManageBac event stream (Ctrl+C to stop)...")
    async for event in daemon.stream():
        print(f"\n[Event: {event.event} @ {event.timestamp}]")
        
        if event.event == "task_created":
            print(f"  📝 New Task: {event.data.get('title')}")
            print(f"     Class: {event.data.get('class_name')}")
            print(f"     Due: {event.data.get('due_date')}")
            print(f"     URL: {event.data.get('url')}")
            
        elif event.event == "deadline_approaching":
            print(f"  ⏰ Deadline Warning: {event.data.get('title')}")
            print(f"     Threshold: {event.data.get('reminder_threshold')}")
            print(f"     Remaining: {event.data.get('time_remaining_minutes')} mins")
            
        elif event.event == "task_graded":
            print(f"  📊 Grade Released: {event.data.get('title')}")
            print(f"     Score: {event.data.get('grade_letter')} {event.data.get('grade_score')}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected from event stream.")
```

---

## Real-Time Webhook Engine

`mb-cli` includes a robust background daemon that can dispatch events to HTTP webhook receivers (e.g. local scripts, microservices, or custom bots):

```bash
# Run daemon in foreground with webhook dispatching
# Pass the secret via the environment so it does not appear in `ps` output
# or your shell history.
export MB_WEBHOOK_SECRET="your-hmac-secret"
mb daemon run --webhook-url http://127.0.0.1:8000/webhook --secret "$MB_WEBHOOK_SECRET"

# Or configure webhook URL persistently and run daemon.
# `start` only detaches when you pass -b/--background; without it the loop runs
# in the foreground and dies with your terminal.
mb daemon configure-webhook http://127.0.0.1:8000/webhook
mb daemon start -b --interval 1800 --active-hours-start 7 --active-hours-end 23

# Test the webhook connection with a mock ping
mb daemon test-webhook http://127.0.0.1:8000/webhook
```

> **Verify the signature.** The daemon signs every payload with HMAC-SHA256,
> but a receiver that ignores `X-MB-Signature` accepts forged events from
> anything that can reach its port. The bundled receiver in
> `extras/mb-notifier/` requires `--secret` (or `MB_WEBHOOK_SECRET`) and refuses
> unsigned, replayed, or stale pushes. Write your receiver the same way.

### Webhook HTTP Contract
- **Method**: `POST`
- **Headers**:
  - `Content-Type: application/json; charset=utf-8`
  - `User-Agent: mb-crawler-daemon/1.0`
  - `X-MB-Event: <event_type>` (e.g. `task_created`, `task_graded`)
  - `X-MB-Signature: sha256=<hex_hmac>` (when `--secret` is configured)
- **Retry Mechanism**: Exponential backoff (`1s`, `2s`, `4s`) on network or server errors.
- **Specification**: See [docs/events.md](docs/events.md) for full payload schemas and documentation.

---

## Interactive CLI Reference

```bash
# Authentication & Session
mb login --school your-school --domain managebac.com -e student@example.com
mb logout

# Tasks & Coursework
mb list                                 # list upcoming tasks
mb list --view past                     # past tasks
mb list --subject "Math"                # filter by class/subject
mb list --view overdue --details        # overdue tasks with full descriptions
mb view 1000025                        # view single task by ID
mb view "https://your-school.managebac.com/student/classes/1000024/core_tasks/1000025"

# File Submission
mb submit 1000026 homework.pdf         # upload file to assignment dropbox

# Submission Lifecycle
mb submissions 1000026 --list          # list current submissions for a task
mb submissions 1000026 --add hw.pdf    # upload to the task dropbox
mb submissions 1000026 --delete hw.pdf # delete a submission by asset ID or filename
mb submissions 1000026 --check-feedback # check teacher feedback (optionally filter by asset ID/name)
mb download 1000026                    # download all attachments + submissions for a task
mb download 1000026 --no-attachments --output-dir ./math  # student submissions only
mb feedback 1000026                    # fetch teacher feedback for a submitted task

# Grades & Analytics
mb grades                               # list all enrolled classes
mb grades --class-id 1000023           # detailed task grades for one class
mb grades --subject "Physics"           # fuzzy match class name
mb count-grade-freq                     # grade distribution across all classes

# Notifications & Feed
mb notifications                        # list MNN notifications (page 1)
mb notifications --read 235151424       # mark notification as read
mb notifications --read-all             # mark all notifications read

# Schedule & Calendar
mb calendar                             # calendar events for next 7 days
mb calendar --today                     # today's events
mb calendar --ical -o calendar.ics      # export raw iCal feed
mb timetable                            # view weekly class timetable

# Background Daemon
mb daemon run --webhook-url http://127.0.0.1:8000/webhook  # foreground loop, Ctrl+C to stop
mb daemon start -b                     # detached background loop (-b / --background)
mb daemon start                        # foreground loop; dies with your terminal
mb daemon start --once                 # run one check cycle and exit
mb daemon stop                         # stop background loop
mb daemon status                       # show daemon process status
mb daemon install                      # register an auto-start service (launchd/systemd)
mb daemon uninstall                    # remove the auto-start service
mb daemon configure-channel qq 123456789  # deliver via a zeroclaw channel instead of HTTP
```

> **`mb daemon start` does not background by default.** Without `-b` /
> `--background` the polling loop runs in the *foreground* and terminates when
> your terminal closes. `start -b` re-executes itself as `mb daemon run` in a
> new session, writes the child's PID to `~/.config/mb-crawler/daemon.pid`, and
> appends output to `~/.config/mb-crawler/daemon.log` (both `0600`). Override
> either location with `--pid-file` / `--log-file`: `start` and `status` accept
> both, `stop` accepts only `--pid-file`, and `install` accepts only
> `--log-file`.
>
> **`--interval` vs `--poll-interval`.** `mb daemon start` takes `--interval`;
> `mb daemon run` takes `--poll-interval`. Same setting, two names — the flag
> names differ for historical reasons and are kept as-is so existing commands do
> not break. `start -b` translates `--interval` into `--poll-interval` when it
> spawns the detached process, so passing both to one command is an error.
>
> **`--daemon-config` is not `--config`.** The daemon subcommands above read
> their webhook URL, interval, and active-hours window from a separate JSON file
> selected with `--daemon-config` — it is not the ManageBac `config.json` that
> `--config` selects, and the two are not interchangeable.

### Output Formatting
- **Interactive TTY**: Formatted tables with color highlights.
- **Piped / Non-TTY**: Structured JSON output.
- **Explicit Override**: Add `--format pretty` or `--format json` to any command.
- **Streams**: Standard output (`stdout`) is reserved for command data; logs and progress go to standard error (`stderr`).

### Configuration Files
By default, `mb-cli` stores credentials and daemon states in `~/.config/mb-crawler/`:
- `config.json` — School domain, preferences, and webhook settings
- `session.json` — Authenticated session cookies and tokens
- `creds.json` — **Plaintext ManageBac password**, stored to allow silent re-login
- `snapshot.json` — Coursework state cache for delta detection
- `daemon.log` / `daemon.pid` — Background daemon runtime files
- `cache/` — Cached HTTP responses, including grade pages and the MNN hub JWT
- `daemon_state.json` — Notification/reminder dedup state

Every file holding a credential or personal data is written with `0600` and the
directory with `0700`.

> **`creds.json` holds your password in cleartext.** It is only written when a
> password login succeeds *without* `--temp`. Use `mb login --temp` for a
> one-off session that is not persisted. `mb logout` clears the session cookie
> and the response cache, but does **not** delete `creds.json` — remove it
> manually if you want the password gone:
> ```bash
> rm ~/.config/mb-crawler/creds.json
> ```

`--config <file>` and `--session-file <file>` override the default config and
session paths, as do the environment variables `MB_CRAWLER_CONFIG`,
`MB_CRAWLER_SESSION`, and `MB_CRAWLER_CREDS_PATH`. These come from the shared
auth-flag helper, so they exist on the task, grades, calendar, and submission
commands — but **not universally**. The daemon subcommands act on the daemon
process and its own JSON settings rather than on your ManageBac login, so none of
them accept `--config` or `--session-file`. Where they need a path they take
`--daemon-config` (on `run`, `stop`, `test-webhook`, `configure-webhook`, and
`configure-channel`) and/or `--pid-file` / `--log-file` (`stop` takes
`--pid-file`, `status` takes both, `install` takes `--log-file`); `uninstall`
takes no path flag at all.

Secrets may also be supplied through the environment, which keeps them out of
your shell history and out of `ps` output:
- `MB_WEBHOOK_SECRET` — HMAC secret for signing webhook payloads. Preferred over
  `--secret` when both are set.
- `MB_CRAWLER_PASSWORD` — ManageBac password.
- `MB_CRAWLER_COOKIE` — `_managebac_session` cookie value.

> `MB_CRAWLER_PASSWORD` and `MB_CRAWLER_COOKIE` are **write-only** in the current
> CLI: `mb daemon start -b` copies them into the detached child's environment so
> the secret never travels in `argv`, but nothing in `mb-cli` reads them back as
> input. Supply a password with `--password` / `-p` or the interactive prompt
> rather than relying on these two.

---

## MCP Server (AI Coding Assistants)

`mb-cli` includes a built-in Model Context Protocol (MCP) server for integration with Claude Desktop, Cursor, Gemini, and other AI agents:

```bash
mb-mcp
```

### Example Claude Desktop Configuration
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "managebac": {
      "command": "mb-mcp"
    }
  }
}
```

The MCP server exposes 14 tools: `list_tasks`, `view_task`, `submit_file`, `delete_submission`, `get_teacher_feedback`, `get_notifications`, `mark_notification`, `mark_all_notifications_read`, `get_calendar_events`, `get_ical_feed`, `get_timetable`, `list_classes`, `get_class_grades`, and `count_grade_frequencies`.

---

## Downstream Integrations

`mb-cli` intentionally avoids coupling itself to specific push providers, notification line limits, or personal course naming conventions. Instead, downstream consumers subscribe to events and apply customized logic:

- **[Event Stream Specification](docs/events.md)**: Full specification of the event data contract, lifecycle states, and JSON payloads.
- **[Downstream Notifier Guide](docs/downstream-notifier-guide.md)**: Operational guide for deploying `extras/mb-notifier` (Bark push alerts, 3-field / 4-line mobile screen budgeting, course aliases, and sound customization).

---

## Stability Note

This tool interfaces with ManageBac via automated HTTP requests and HTML parsing. If ManageBac updates its frontend layout, CSS selectors, or internal API structures, scrapers may require updates.

---

## Changelog & Security

Release history follows [Keep a Changelog](https://keepachangelog.com/) in [CHANGELOG.md](CHANGELOG.md). For reporting a vulnerability privately, see [SECURITY.md](SECURITY.md) — it also documents exactly which credentials are stored on disk and in what form.

---

## License

MIT