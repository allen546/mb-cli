# Submissions Lifecycle Management Design

**Author**: Allen & Antigravity  
**Date**: 2026-09-13  
**Status**: Draft / Under Review  

---

## 1. Overview

ManageBac allows students to submit files to a task's dropbox and remove them before the assignment's due date. Currently, `mb-cli` only supports submitting (`mb submit` / `client.submit_file`) and downloading feedback (`mb feedback`).

This specification defines a unified submissions management interface across the client, CLI, and MCP layers:
```bash
mb submissions <task-id> [--list | --add <file> | --delete <asset> | --check-feedback [asset]]
```
It supports listing submissions with asset IDs and timestamps, uploading new files, deleting submitted files (with strict verification against server-side deadline rollbacks), and inspecting teacher feedback.

---

## 2. Background & Problem Statement

### 2.1 The Problem
When a user accidentally submits a file to the wrong task (or needs to replace an uploaded file):
1. **Upcoming Tasks**: The ManageBac web interface exposes a delete button (`a.btn-remove`) that triggers:
   ```http
   DELETE /student/dropboxes/:dropbox_id/destroy_asset?file_id=:file_id
   ```
2. **Past Tasks**: If the deadline has elapsed, the UI hides the delete button. If the `destroy_asset` endpoint is called directly on a past-due task, the server returns HTTP 200 with a flash message `"Success. File was successfully deleted."`, but Rails model callbacks abort the transaction, silently keeping the file in the database.
3. `mb-cli` lacked:
   - A way to inspect existing submission asset IDs from the terminal.
   - A `delete_submission` method to delete submitted files.
   - The `mb submissions` command outlined in `SPEC.md`.

---

## 3. Architecture & Components

### 3.1 Client Layer (`src/mb_cli/client.py`)

#### `get_submissions(class_id: str, task_id: str) -> list[dict]`
Enhance the existing parser to extract rich metadata for each submission row:
* `asset_id`: Numeric asset string extracted from row `id="asset_<id>"` or S3 URL `/uploads/asset/file/<id>/`.
* `name`: Display filename.
* `url`: Direct link to the submission.
* `uploaded_at`: String formatted timestamp (e.g. `"Sep 13, 2026 at 4:30 PM"`), extracted from `<label>Uploaded ...</label>`.
* `can_delete`: Boolean indicating if `a.btn-remove` / `fi-trash` is rendered in the row for this submission.
* `delete_url`: Extracted deletion URL (e.g. `"/student/dropboxes/17874401/destroy_asset?file_id=82189817"`).
* `feedback_url`: Link to feedback if available.
* `preview_modal_url`: Link to PDF preview modal if available.

#### `delete_submission(class_id: str, task_id: str, asset_identifier: str) -> dict`
Implements submission deletion:
1. **Fetch Task Page**: Fetches task page with `bypass_cache=True`.
2. **Extract CSRF & Dropbox ID**:
   * Reads `meta[name="csrf-token"]`.
   * Finds dropbox ID from `form[id^="edit_dropbox_"]` or existing `delete_url`s.
3. **Resolve Asset**:
   * Matches `asset_identifier` against `asset_id` (e.g. `"82189817"`, `"asset_82189817"`) or `name` (case-insensitive filename match).
   * If not found, raises `ValueError(f"Submission not found: {asset_identifier}")`.
4. **Execute Deletion Request**:
   * Issues `DELETE` to `{self.base}/student/dropboxes/{dropbox_id}/destroy_asset?file_id={asset_id}`.
   * Headers: `X-CSRF-Token`, `X-Requested-With: XMLHttpRequest`, `Referer: {task_url}`.
5. **Post-Delete Verification**:
   * Fetches fresh task HTML without cache.
   * Checks whether `asset_id` is still listed.
   * If missing: Successfully deleted! Invalidates cache via `self.invalidate_task_cache(class_id, task_id)` and returns:
     ```python
     {"ok": True, "asset_id": asset_id, "filename": name, "status": "deleted"}
     ```
   * If still present: Raises `RuntimeError("File was not deleted: task deadline has passed and ManageBac server locked the submission.")`.

---

### 3.2 CLI Interface (`src/mb_cli/__main__.py`)

#### Command Syntax
```bash
mb submissions [target] [--id TASK_ID] [--list] [--add FILE_PATH] [--delete ASSET] [--check-feedback [ASSET]]
```

#### Behavior & Arguments
* `target` (positional) or `--id TASK_ID`: Identifies the task. Can be a numeric ID (e.g. `1000099`) or full URL.
* If `target` is specified without action flags, default to `--list`.
* If no `target` and no action flags are specified, print an error and exit with code 1.
* Actions:
  * `--list`: List submissions in a formatted table or JSON.
  * `--add <path>` (alias `--submit`): Uploads a file via `client.submit_file`.
  * `--delete <asset>`: Deletes the asset via `client.delete_submission`.
  * `--check-feedback [asset]`: Displays feedback and rubric details via `client.get_task_feedback`.
* Global flags: `--format pretty|json`, `--profile`, `--config`, `--session-file`, `--refresh`.

#### Snapshot Updates
* When a submission is deleted:
  * Call `client.invalidate_task_cache(class_id, task_id)`.
  * Re-fetch remaining submissions. If no submissions remain, update snapshot status to `"not-submitted"` and `has_submit_button = True`.
* When a submission is added:
  * Update snapshot status to `"submitted"`.

---

### 3.3 Formatting Layer (`src/mb_cli/formatters.py`)

* `format_submissions_list(task_info: dict, submissions: list[dict]) -> str`:
  Formats a table with headers: `Asset ID`, `File Name`, `Uploaded At`, `Deletable`, `Feedback`.
* `format_submission_deletion(result: dict) -> str`:
  Formats success/failure indicators.

---

### 3.4 MCP Server Integration (`src/mb_cli/mcp_server.py`)

Register tool `delete_submission`:
```python
@mcp.tool()
def delete_submission(
    task_id: str,
    asset_id: str,
    school: str | None = None,
    domain: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    verify_tls: bool = True,
    retry: int = 3,
) -> str:
    """Delete a submitted file from a task's dropbox on ManageBac."""
```
Returns structured JSON on success or descriptive error string on failure.

---

## 4. Error Handling & Edge Cases

| Case | Handling |
|------|----------|
| No task ID provided and no action | Print clear CLI error explaining usage. |
| Task not found | Raise task resolution error (`invalid_target`). |
| Non-existent asset ID/filename | Return error `Submission not found: <asset>`. |
| Task deadline passed (server silent rollback) | Detect through post-delete verification and raise `RuntimeError("File was not deleted: task deadline has passed...")`. |
| No submissions exist on task | Print friendly message: `"No submissions found for this task."` |
| Multiple files matching filename | Match exact filename first; if ambiguous, require asset ID. |

---

## 5. Testing Plan

1. **Unit Tests (`tests/test_submissions.py`)**:
   * Test `get_submissions` HTML parsing with full metadata (`asset_id`, `uploaded_at`, `can_delete`, `delete_url`).
   * Test `delete_submission` success path with mock HTTP DELETE and cache invalidation.
   * Test `delete_submission` post-verification failure when file remains on page.
   * Test `delete_submission` with filename vs asset ID.
2. **CLI Tests (`tests/test_cli_submissions.py`)**:
   * Test `mb submissions <id>` defaulting to `--list`.
   * Test `mb submissions <id> --list`.
   * Test `mb submissions <id> --add <file>`.
   * Test `mb submissions <id> --delete <asset>`.
   * Test `mb submissions <id> --check-feedback`.
   * Test missing task ID validation.
3. **MCP Tool Tests (`tests/test_mcp_submissions.py`)**:
   * Test MCP `delete_submission` tool execution.
