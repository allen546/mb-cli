# mb-cli Use Case: Homework Inbox Triage and Submission Prep

## Goal

This use case demonstrates how a student can use `mb-cli` to turn ManageBac into a command-line homework inbox. The workflow helps the student:

- sign in without exposing credentials in shell history
- find overdue and upcoming homework
- inspect each task's requirements
- collect task instructions and attached resources into local work folders
- submit finished files back to the task dropbox
- check supporting context such as notifications, calendar events, timetable, and grades

All account-specific values in this document are placeholders. Do not put real emails or passwords in this file.

## Privacy and Safety Setup

Use environment variables to keep demo/session files outside the repo, and let `mb login` prompt for the password instead of passing it inline.

```powershell
$env:MB_CRAWLER_CONFIG = "$PWD\.local-demo\config.json"
$env:MB_CRAWLER_SESSION = "$PWD\.local-demo\session.json"
```

Recommended `.gitignore` entry for local demos:

```gitignore
.local-demo/
```

## Installation

```powershell
git clone https://github.com/allen546/mb-cli.git
cd mb-cli
pip install .
```

## 1. Log In

Use the school subdomain and ManageBac domain. The command prompts for the password, so no password is written into terminal history.

```powershell
mb login --school <school-subdomain> --domain <managebac.com-or-managebac.cn> -e <student-email>
```

Example with placeholders:

```powershell
mb login --school <school-subdomain> --domain managebac.cn -e <student-email>
```

## 2. Find Actionable Homework

List overdue tasks:

```powershell
mb list --view overdue --details
```

List upcoming tasks:

```powershell
mb list --view upcoming --details
```

For scripting or filtering, use JSON:

```powershell
mb list --view overdue --details --format json > overdue.json
mb list --view upcoming --details --format json > upcoming.json
```

What to look for:

- tasks labeled `Pending`
- tasks with no submission listed in their dropbox
- tasks due soon or already overdue

## 3. Inspect One Homework Task

After finding a task ID from `mb list`, inspect it:

```powershell
mb view <task-id>
```

JSON output is useful for automation:

```powershell
mb view <task-id> --format json > task.json
```

The useful fields are:

- task title
- class name
- due date
- description / instructions
- attachment URLs
- submission/dropbox text

## 4. Create Local Work Folders

Create a workspace for homework files:

```powershell
New-Item -ItemType Directory -Force "$HOME\Desktop\MBWork" | Out-Null
```

Create one folder per task using the due date and title:

```powershell
New-Item -ItemType Directory -Force "$HOME\Desktop\MBWork\Jun17-Character Webs" | Out-Null
```

Save the task request into the folder:

```powershell
mb view <task-id> > "$HOME\Desktop\MBWork\Jun17-Character Webs\task_req.txt"
```

If using JSON, the same idea can be automated by reading `detail.description` and writing it to `task_req.txt`.

## 5. Collect Attached Assets

`mb view <task-id> --format json` exposes attachment metadata and URLs when ManageBac shows files on the task page. A student can use those links to download worksheets, PDFs, templates, rubrics, or other resources into the matching folder.

Current workflow:

```powershell
mb view <task-id> --format json > task.json
```

Then download the listed attachment URLs into:

```text
$HOME\Desktop\MBWork\<DueDate-TaskTitle>\
```

Possible future enhancement for `mb-cli`:

```powershell
mb assets <task-id> --output "$HOME\Desktop\MBWork\Jun17-Character Webs"
```

This would make asset download a first-class command instead of requiring a helper script.

## 6. Submit Finished Homework

After completing the assignment file locally:

```powershell
mb submit <task-id> "$HOME\Desktop\MBWork\Jun17-Character Webs\finished-homework.pdf"
```

For a task URL instead of an ID:

```powershell
mb submit "https://<school-subdomain>.<domain>/student/classes/<class-id>/core_tasks/<task-id>" ".\finished-homework.pdf"
```

## 7. Supporting Checks

Check unread notifications:

```powershell
mb notifications
```

Check the next week of calendar events:

```powershell
mb calendar
```

Check today's timetable:

```powershell
mb timetable --today
```

List classes before checking grades:

```powershell
mb grades
```

Inspect one class:

```powershell
mb grades --class-id <class-id>
```

## Commands Exercised

| Command | Purpose in this use case |
| --- | --- |
| `mb login` | Start an authenticated ManageBac session |
| `mb list --view overdue` | Find overdue homework |
| `mb list --view upcoming` | Find upcoming homework |
| `mb view <task-id>` | Read task requirements and attachment metadata |
| `mb submit <task-id> <file>` | Upload completed work |
| `mb notifications` | Check reminders and updates |
| `mb calendar` | Cross-check due dates and events |
| `mb timetable` | See daily schedule context |
| `mb grades` | Review classes and grade context |
| `mb logout` | Clear saved session data when done |

## Outcome

This use case shows that `mb-cli` can support a real student workflow: turn ManageBac tasks into a local homework workspace, keep task requirements beside the work files, and submit finished assignments from the command line.

It also highlights a useful next feature: a built-in command to download task attachments directly into a chosen folder.
