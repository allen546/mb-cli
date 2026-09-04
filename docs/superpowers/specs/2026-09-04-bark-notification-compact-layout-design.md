# Compact Bark Notification Layout & Course Aliases Design

## 1. Problem & Context
ManageBac push notifications dispatched via `bark_webhook_receiver.py` to Bark (iOS / macOS) are prone to overflowing:
- iOS notification banners permit **at most 2 lines for the title** and **at most 4 lines for the body**.
- Previously, the receiver repeated course and task names across both the title and body (`课程: ...`, `作业: ...`, `教师: ...`, `截止: ...`).
- When course names or task titles are long, lines wrap. A 4-item list where lines wrap quickly exceeds the 4-line physical limit, causing iOS to truncate critical information (such as deadline or teacher).
- Teacher information adds unnecessary noise and consumes vertical space.
- ManageBac course names cannot be heuristically shortened without risking unintended corruption (e.g. `honorable` becoming `honsable`).

## 2. Requirements & Constraints
1. **Zero Class Autocleaning**: Do not use regex heuristics, year/grade stripping, or substring pattern replacements on course names. The raw course name provided by ManageBac is preserved as-is (trimmed only of outer whitespace).
2. **Configurable Exact Course Aliases**:
   - Shortening is 100% delegated to an external alias mapping.
   - Matching is strictly **case-sensitive exact match**.
   - If a course name matches an alias key, it is replaced by the alias; otherwise, the raw course name is kept unmodified.
3. **Teacher Omission**: Teacher names are completely omitted across all notification templates.
4. **English Alert Titles**: Clean, concise alert titles in English (e.g., `📝 New Task`, `⏰ DDL Warning`), without literal markdown syntax like `#`.
5. **3-Field Layout & 4-Visual-Line Budget**:
   - Body contains exactly 3 logical fields:
     - Field 1: `课程: {course}` (budgeted for 1 visual line)
     - Field 2: `作业: {task}` (budgeted for up to 2 visual lines)
     - Field 3: Event-specific metadata, such as `截止: ...` or `得分: ...` (budgeted for 1 visual line)
   - Total visual line footprint: $1 + 2 + 1 = 4$ lines max, guaranteeing that deadlines and critical status lines are never pushed off screen.

---

## 3. Architecture & Components

### 3.1 Course Alias Configuration
- **File Location**: `~/.config/managebac/course_aliases.json`
  - Overridable via CLI argument `--course-aliases <path>`.
  - If the file does not exist or contains invalid JSON, the system gracefully falls back to using the raw course name unmodified without crashing.
- **Format**:
  ```json
  {
    "English Language Arts Hons": "ELA Hons",
    "AP Computer Science A": "AP CSA",
    "AP Calculus BC": "AP Calc BC"
  }
  ```
- **Resolution Flow**:
  ```python
  def resolve_course_name(raw_name: str | None, aliases: dict[str, str]) -> str:
      if not raw_name:
          return "ManageBac"
      trimmed = str(raw_name).strip()
      return aliases.get(trimmed, trimmed)
  ```

### 3.2 Visual Line Budgeting & Field Clamping
To prevent wrapping beyond 4 lines on mobile displays (e.g., iPhone Bark notifications):
- **Title**: English event label (under 30 characters). Never wraps past 1 line.
- **Field 1 (`课程: {course}`)**:
  - Clamped to at most 40 characters with safe `..` truncation if an unaliased course name is excessively long.
  - Guarantees Field 1 consumes exactly 1 visual line.
- **Field 2 (`作业: {task}`)**:
  - Allocated up to 80 characters (equivalent to 2 full visual lines on standard mobile screens).
  - Allows long assignment titles to wrap across 2 physical lines naturally.
  - Truncated with `..` only if exceeding 80 characters.
- **Field 3 (`截止: ...` / `得分: ...` / etc.)**:
  - Clamped to at most 40 characters.
  - Consumes exactly 1 visual line at the bottom.

---

## 4. Event Templates

| Event Type | Title | Field 1 (1 line) | Field 2 (up to 2 lines) | Field 3 (1 line) | Sound & Priority |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `task_created`, `new_task` | `📝 New Task` | `课程: {course}` | `作业: {task}` | `截止: {due}` | `bell`, priority 6 |
| `deadline_approaching` | `⏰ DDL Warning` | `课程: {course}` | `作业: {task}` | `截止: {due} (仅剩 {time_left})` | `alarm`, priority 10 |
| `task_updated`, `updated_task` | `✏️ Updated Task` | `课程: {course}` | `作业: {task}` | `截止: {due}` *(or `状态: 已提交`)* | `bell`, priority 5 |
| `assignment_graded`, `grade_posted` | `📊 Grade Posted` | `课程: {course}` | `作业: {task}` | `得分: {score}` | `chime`, priority 7 |
| `file_uploaded`, `new_file_uploaded` | `📁 File Uploaded` | `课程: {course}` | `课件: {filename}` | `上传: {time}` | `bell`, priority 5 |
| `announcement_created`, `new_announcement` | `📢 Class Announcement` | `课程: {course}` | `主题: {title}` | `发布: {time}` | `bell`, priority 5 |
| `test_ping` | `🔔 Test Notification` | `通道: 实时推送正常` | `设备: Mac & iPhone` | `时间: {time}` | `bell`, priority 5 |
| Other / Fallback | `ManageBac Notification` | `课程: {course}` | `内容: {task}` | `时间: {time}` | `bell`, priority 5 |

---

## 5. Implementation Changes

### `bark_webhook_receiver.py`
1. **Remove Heuristic Course Cleaning**:
   - Deprecate/replace `clean_class_name` with `resolve_course_name(raw_name, aliases)`.
   - Remove regex stripping of years, grade numbers, repeated `AP AP—`, and color codes.
2. **Add Alias Loader**:
   - Function `load_course_aliases(path: Path | str | None) -> dict[str, str]`.
   - Caches aliases by file `mtime` so updates to `course_aliases.json` take effect immediately without restarting the receiver.
3. **Update CLI Arguments**:
   - Add `--course-aliases` parameter (defaults to `~/.config/managebac/course_aliases.json`).
4. **Refactor `format_event_for_bark`**:
   - Accept `aliases: dict[str, str]` parameter (or pass loaded alias provider).
   - Format titles with English labels.
   - Remove `clean_teacher_name` calls and omit teacher from message body.
   - Format 3 logical fields: `课程:`, `作业:` (up to 80 chars), and the status/DDL field (up to 40 chars).
   - Join with `\n` to produce at most 3 logical lines (and at most 4 rendered visual lines).

---

## 6. Verification Plan

### Automated Unit Tests
1. **Alias Resolution Tests**:
   - Test exact case-sensitive match returns mapped alias.
   - Test non-matching course returns raw name unmodified (no autocleaning).
   - Test case mismatch (e.g. lowercase vs uppercase) does not match and returns raw name unmodified.
   - Test missing file or malformed JSON handles error gracefully and returns raw name.
2. **Formatting & Line Budget Tests**:
   - Test each event type produces expected English title and 3-field body.
   - Test absence of teacher in output.
   - Test short task titles stay on 1 line (total 3 lines).
   - Test long task titles utilize up to 80 chars and truncate cleanly if longer.
   - Test course name clamps to 40 chars if unaliased.
3. **Webhook Receiver Integration Test**:
   - Post simulated webhook events to `WebhookHandler` and verify `Pusher.push()` is called with formatted title and message.
