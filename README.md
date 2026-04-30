# mb-cli

Crawl **ManageBac** tasks, grades, submissions, notifications, calendar, and timetable from the command line or via MCP.

Supports both `managebac.com` and `managebac.cn` (China) instances.

## Disclaimer

**Use at your own risk.** This tool is an unofficial, community-maintained scraper. It is not affiliated with or endorsed by Faria Education or ManageBac. By using this tool, you acknowledge and accept the following:

Faria/ManageBac's legal documents explicitly prohibit automated access:

- **robots.txt** (managebac.com): Disallows `/login`, `/admin`, `/api` for all user agents. ~~doesn’t exist for managebac.cn~~
- **Terms of Use §1.2.6**: "Accounts registered by 'bots' or screen scrapers and/or other automated means are not permitted and access will be terminated without notice."
- **Terms of Service §5.5**: "Misuse of the Service, including but not limited to reverse engineering... may result in permanent and/or temporary suspension or termination of the School's account."
- **Terms of Service §1.4**: Violations may result in account termination without notice.
- **Terms of Service §9.4**: Schools exceeding 200 GB/month bandwidth may face caps or additional invoices.

**The author of this tool bears no responsibility for any consequences resulting from its use, including but not limited to account suspension, termination, or school-level penalties.** You are solely responsible for ensuring your use complies with your school's policies and ManageBac's Terms of Service.

## Install

```bash
pip install .
```

## Commands

```bash
mb login
mb list
mb view
mb submit
mb notifications
mb calendar
mb timetable
mb grades
mb count-grade-freq
mb logout
mb daemon start
mb daemon stop
mb daemon configure-webhook
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

This starts the server on stdio transport with 12 tools: `list_tasks`, `view_task`, `submit_file`, `get_notifications`, `mark_notification`, `mark_all_notifications_read`, `get_calendar_events`, `get_ical_feed`, `get_timetable`, `list_classes`, `get_class_grades`, `count_grade_frequencies`.

## Config files

By default, `mb-cli` stores JSON files in `~/.config/mb-crawler/`:

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
mb login --school bj80 --domain managebac.cn -e you@example.com
```

Or with password inline:

```bash
mb login --school bj80 --domain managebac.cn -e you@example.com -p yourpassword
```

Or with an existing session cookie:

```bash
mb login --school bj80 --domain managebac.cn -c "YOUR_COOKIE_VALUE"
```

Note that this will invalidate the session of the browser you obtained the cookie from.

## List tasks

```bash
mb list
mb list --view past
mb list --subject EL
mb list --details
mb list --view overdue --details
```

## View one task

```bash
mb view 27080372
mb view "https://bj80.managebac.cn/student/classes/11460718/core_tasks/27080372"
```

## Submit files

Upload a file to a task's dropbox:

```bash
mb submit 27254393 homework.pdf
mb submit "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393" homework.pdf
```

## Notifications

View and manage notifications via the MNN Hub API:

```bash
mb notifications                         # list (page 1)
mb notifications --page 2 --per-page 10  # pagination
mb notifications --read 235151424         # mark as read
mb notifications --read-all               # mark all as read
mb notifications --unread 235151424       # mark as unread
```

Unread notifications are marked with `*`.

## Calendar

View calendar events via the JSON API or raw iCal feed:

```bash
mb calendar                           # next 7 days
mb calendar --start 2026-05-01 --end 2026-05-07
mb calendar --today                   # today only
mb calendar --ical                    # raw iCal feed output
mb calendar --ical -o calendar.ics   # save iCal to file
```

## Timetable

View the weekly timetable (HTML scrape):

```bash
mb timetable                          # this week
mb timetable --date 2026-04-28        # week starting from date
mb timetable --today                  # this week
```

Current day is marked with `*`.

## Grades

View all grades for a class and its expected grade:

```bash
mb grades                              # list all classes
mb grades --class-id 11460711          # detailed grades for one class
mb grades --subject EL                 # fuzzy match class name
```

Shows per-task grades, category weights, and a computed expected grade.

## Count grade frequency

Count how many times each grade letter appears across all or one class:

```bash
mb count-grade-freq                    # all classes
mb count-grade-freq --subject EL       # one class only
```

## Daemon

Configure webhook:

```bash
mb daemon configure-webhook http://127.0.0.1:42617/webhook
```

Run one daemon cycle without posting:

```bash
mb daemon start --once --dry-run
```

Run loop mode:

```bash
mb daemon start
mb daemon start --interval 1800                        # 30 min base (randomized ±20%)
mb daemon start --active-hours-start 8 --active-hours-end 22  # only check 8am-10pm
```

The daemon randomizes its polling interval (±20% of base) and only runs during active hours (default 7am-11pm local time). Outside active hours it sleeps 10 minutes between checks.

Stop loop mode:

```bash
mb daemon stop
```

## Logout

```bash
mb logout
mb logout --all
```

## Library usage

```python
from mb_cli import ManageBacClient, MNNHubClient
from mb_cli.notifications import hub_for_domain

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

## Stability note

This tool is fundamentally a web scraper. It parses ManageBac HTML pages and relies on internal markup structure (CSS classes, DOM layout). If Faria Education changes their frontend, parsing may break without warning. The student name heuristic (`_capture_student_name`) is particularly fragile — it looks for a profile link with specific text patterns and may fail silently if the page layout changes.

## License

MIT