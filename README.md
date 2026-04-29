# mb-crawler

Crawl **ManageBac** tasks, grades, submissions, notifications, calendar, and timetable from the command line or via MCP.

Supports both `managebac.com` and `managebac.cn` (China) instances.

## Install

```bash
pip install .
```

## Commands

```bash
mb-crawler login
mb-crawler list
mb-crawler view
mb-crawler submit
mb-crawler notifications
mb-crawler calendar
mb-crawler timetable
mb-crawler grades
mb-crawler logout
mb-crawler daemon start
mb-crawler daemon stop
mb-crawler daemon configure-webhook
```

Output defaults:

- interactive TTY: `pretty`
- non-interactive / piped: `json`
- override with `--format pretty` or `--format json`

Stdout is reserved for command output. Stderr is reserved for crawl/auth progress logs.

## MCP Server

An MCP (Model Context Protocol) server is also available for AI agent integration:

```bash
mb-mcp
```

This starts the server on stdio transport with 11 tools: `list_tasks`, `view_task`, `submit_file`, `get_notifications`, `mark_notification`, `mark_all_notifications_read`, `get_calendar_events`, `get_ical_feed`, `get_timetable`, `list_classes`, `get_class_grades`.

## Config files

By default, `mb-crawler` stores JSON files in `~/.config/mb-crawler/`:

- `config.json`
- `session.json`
- `daemon.json`
- `snapshot.json`
- `daemon.pid`
- `daemon.log`

Override config/session paths with:

- `--config /path/to/config.json`
- `--session-file /path/to/session.json`
- `MB_CRAWLER_CONFIG`
- `MB_CRAWLER_SESSION`

## Login

```bash
mb-crawler login --school bj80 --domain managebac.cn -e you@example.com
```

Or with password inline:

```bash
mb-crawler login --school bj80 --domain managebac.cn -e you@example.com -p yourpassword
```

Or with an existing session cookie:

```bash
mb-crawler login --school bj80 --domain managebac.cn -c "YOUR_COOKIE_VALUE"
```

Note that this will invalidate the session of the browser you obtained the cookie from.

## List tasks

```bash
mb-crawler list
mb-crawler list --view past
mb-crawler list --subject EL
mb-crawler list --details
mb-crawler list --view overdue --details
```

## View one task

```bash
mb-crawler view 27080372
mb-crawler view "https://bj80.managebac.cn/student/classes/11460718/core_tasks/27080372"
```

## Submit files

Upload a file to a task's dropbox:

```bash
mb-crawler submit 27254393 homework.pdf
mb-crawler submit "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393" homework.pdf
```

## Notifications

View and manage notifications via the MNN Hub API:

```bash
mb-crawler notifications                         # list (page 1)
mb-crawler notifications --page 2 --per-page 10  # pagination
mb-crawler notifications --read 235151424         # mark as read
mb-crawler notifications --read-all               # mark all as read
mb-crawler notifications --unread 235151424       # mark as unread
```

Unread notifications are marked with `*`.

## Calendar

View calendar events via the JSON API or raw iCal feed:

```bash
mb-crawler calendar                           # next 7 days
mb-crawler calendar --start 2026-05-01 --end 2026-05-07
mb-crawler calendar --today                   # today only
mb-crawler calendar --ical                    # raw iCal feed output
mb-crawler calendar --ical -o calendar.ics   # save iCal to file
```

## Timetable

View the weekly timetable (HTML scrape):

```bash
mb-crawler timetable                          # this week
mb-crawler timetable --date 2026-04-28        # week starting from date
mb-crawler timetable --today                  # this week
```

Current day is marked with `*`.

## Grades

View all grades for a class and its expected grade:

```bash
mb-crawler grades                              # list all classes
mb-crawler grades --class-id 11460711          # detailed grades for one class
mb-crawler grades --subject EL                 # fuzzy match class name
```

Shows per-task grades, category weights, and a computed expected grade.

## Daemon

Configure webhook:

```bash
mb-crawler daemon configure-webhook http://127.0.0.1:42617/webhook
```

Run one daemon cycle without posting:

```bash
mb-crawler daemon start --once --dry-run
```

Run loop mode:

```bash
mb-crawler daemon start
mb-crawler daemon start --interval 900
```

Stop loop mode:

```bash
mb-crawler daemon stop
```

## Logout

```bash
mb-crawler logout
mb-crawler logout --all
```

## Library usage

```python
from mb_crawler import ManageBacClient, MNNHubClient
from mb_crawler.notifications import hub_for_domain

client = ManageBacClient("bj80", domain="managebac.cn")
client.login("you@example.com", "password")

# Tasks
result = client.crawl_all(fetch_details=True)

# Calendar
events = client.get_calendar_events("2026-04-29", "2026-05-05")

# Timetable
timetable = client.get_timetable()

# Grades
grades = client.get_class_grades("11460711")

# Notifications
hub_endpoint, token = client.get_notification_token()
hub = MNNHubClient(hub_for_domain(client.domain), token)
notifications = hub.list()
hub.mark_read(235151424)

# File submission
client.submit_file("11460711", "27254393", "/path/to/file.pdf")
```

## License

MIT
