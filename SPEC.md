# mb — ManageBac CLI & MCP Server Specification

## Overview

`mb` is a CLI and MCP (Model Context Protocol) server for ManageBac, the Faria
school management platform. It provides:
- **CLI** — daily-use commands for terminal
- **MCP server** — tool interface for AI agents (stdio transport)
- **Shared library** — `mb_crawler` package powers both interfaces

## ManageBac URL Map

All relative to `https://{school}.{domain}` (e.g. `https://bj80.managebac.cn`).

### Core Pages

| Page | Path | Notes |
|------|------|-------|
| Login | `/login` → POST `/sessions` | CSRF token + email/password |
| Tasks & Deadlines | `/student/tasks_and_deadlines?view={upcoming|past|overdue}&page={n}` | Paginated lists |
| Task Detail | `/student/classes/{class_id}/core_tasks/{task_id}` | Description, dropbox, submissions |
| Task Discussions | `/student/classes/{class_id}/core_tasks/{task_id}/discussions` | Comments thread |
| Task Dropbox | `/student/classes/{class_id}/core_tasks/{task_id}/dropbox` | File uploads |

### Extended Pages

| Page | Path | Notes |
|------|------|-------|
| Notifications | `/student/notifications` | Embedded data via mnn-hub |
| Calendar | `/student/calendar` | FullCalendar; iCal feed available |
| Calendar iCal | `/student/events/token/{token}.ics` | HTTP-fetchable iCalendar |
| Timetables | `/student/timetables` | Weekly schedule (HTML scrape) |
| Profile | `/student/profile` | Student name, timezone |

### Notification Architecture

Notifications come from **`mnn-hub.prod.faria.cn`** (Faria notification hub).
The `/student/notifications` page embeds:
- `data-token` — JWT for hub authentication
- `data-mnn-hub-endpoint` — WebSocket server URL
- Notification list is rendered client-side; we scrape the HTML page.

### Calendar Architecture

The `/student/calendar` page loads a FullCalendar component. Events are fetched
via internal AJAX. The page also embeds a **stable iCal feed token** visible as a
`webcal://` link.  Replace `webcal://` with `https://` for direct HTTP fetch.

### Timetable Architecture

`/student/timetables` renders the weekly schedule as HTML. No JSON API was found.
We scrape the rendered page to extract periods/classes/times.

---


## CLI Commands (Proposed)

Installed command: `mb`

### `mb login`
Authenticate and persist config/session.

```
mb login --school bj80 --domain managebac.cn -e user@example.com
mb login --school bj80 -e user@example.com -c "cookie_value"
```

| Flag | Description |
|------|-------------|
| `--school` | School subdomain (required) |
| `-d, --domain` | Base domain (default: managebac.com) |
| `-e, --email` | Login email |
| `-p, --password` | Password (prompted if omitted) |
| `-c, --cookie` | Session cookie instead of password login |
| `--refresh` | Force re-auth even if session exists |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

### `mb list`
List tasks with filtering.

```
mb list
mb list --subject EL --view overdue
mb list --details --view past
mb list --pages 20 --view all
```

| Flag | Description |
|------|-------------|
| `-s, --subject` | Filter by subject/class name |
| `--view` | all (default), upcoming, past, overdue |
| `--details` | Fetch per-task detail pages |
| `--pages` | Max pages per view (default: 10) |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

### `mb view`
Inspect one task in detail.

```
mb view 27080372
mb view https://bj80.managebac.cn/student/classes/11460718/core_tasks/27080372
mb view https://bj80.managebac.cn/.../core_tasks/27080372/discussions
mb view --subject EL 27080372
```

| Flag | Description |
|------|-------------|
| `target` | Task id or URL (positional) |
| `--id` | Task id |
| `--url` | Task or discussion URL |
| `--subject` | Narrow by subject when resolving by id |
| `--pages` | Max pages when resolving by id (default: 10) |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

Discussion URLs are auto-normalized to the main task page.

### `mb submissions`
Submit work to a task dropbox.

```
mb submissions --id 27080372
mb submissions 27080372
mb submissions --id 27080372 --submit file.docx
mb submissions --id 27080372 --list
mb submissions --id 27080372 --delete asset_80753784
```

| Flag | Description |
|------|-------------|
| `target` | Task id (positional) |
| `--id` | Task id |
| `--submit` | Path to file to upload |
| `--list` | List current submissions (default action) |
| `--delete` | Delete submission by asset id |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

### `mb notifications`
Fetch recent notifications.

```
mb notifications
mb notifications --limit 10
```

| Flag | Description |
|------|-------------|
| `--limit` | Max notifications (default: 20) |
| `--unread-only` | Only unread notifications |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

### `mb calendar`
Fetch calendar events for a date range.

```
mb calendar
mb calendar --from 2026-05-01 --to 2026-05-07
mb calendar --today
```

| Flag | Description |
|------|-------------|
| `--from` | Start date (YYYY-MM-DD, default: today) |
| `--to` | End date (default: +7 days) |
| `--today` | Shorthand for today only |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

Implementation: fetch the iCal feed at `/student/events/token/{token}.ics`,
parse VEVENTs, filter by date range, return structured JSON.

### `mb timetable`
Fetch today"s timetable (or a specific date).

```
mb timetable
mb timetable --date 2026-05-01
mb timetable --week
```

| Flag | Description |
|------|-------------|
| `--date` | Date to query (YYYY-MM-DD, default: today) |
| `--week` | Show full week starting from that date |
| `--refresh` | Force re-auth |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

Implementation: fetch `/student/timetables`, scrape the rendered schedule HTML
for the requested date range, return structured periods.

### `mb daemon`
Manage the webhook alert daemon.

```
mb daemon start
mb daemon start --once --dry-run
mb daemon start --interval 900
mb daemon stop
mb daemon configure-webhook http://127.0.0.1:42617/webhook
```

See `SPEC.md` daemon section for full subcommand details (already implemented).

### `mb logout`
Clear persisted session.

```
mb logout
mb logout --all
```

| Flag | Description |
|------|-------------|
| `--all` | Remove all saved sessions |
| `--config` | Path to config JSON |
| `--session-file` | Path to session JSON |
| `--profile` | Profile name |
| `-o, --output` | Write output to file |
| `--format` | pretty (default for TTY) or json |

---

