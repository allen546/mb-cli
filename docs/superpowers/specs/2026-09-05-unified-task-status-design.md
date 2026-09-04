# Unified Task Status, Lifecycle, and CLI Presentation Design

**Date:** 2026-09-05  
**Status:** In Review  
**Author:** Allen Sun & Antigravity  

---

## 1. Objective

Establish a single, authoritative domain model for task submission status, grading status, completion/todo lifecycle, view classification, and presentation formatting across `mb-cli`. Eliminate redundant, divergent, and contradictory heuristics in `formatters.py`, `filters.py`, `client.py`, and `daemon/`.

---

## 2. Problem Statement & Root Cause

Currently, task status and completion are evaluated in multiple places using divergent logic:

1. **`formatters.py:get_grade_display`**:
   - Inside `render_pretty(list)`: hardcoded local helper that formats grades or falls back to `"Ungraded"`, `"Unsubmitted"`, or `"⚠ Unsubmitted"`.
   - Inaccessible to filters or other commands.
2. **`formatters.py:render_pretty(view)`**:
   - Formats `grade_display` as `"None"` (ignoring `"Ungraded"` or `"Unsubmitted"`).
   - Formats `status_display` as `"Complete"` or `"Incomplete (Todo)"` using different logic from `list`.
3. **`filters.py:is_task_unfinished`**:
   - Strictly requires `has_submit_button=True`.
   - Suffers from a double-negative bug (`and not is_not_assessed`), treating any task marked `"Not Assessed Yet"` by the teacher as complete/exempt.
   - Ignores `status == "not-submitted"`.
4. **`filters.py:matches_graded`**:
   - Considers any task with `grade_letter` as graded, including `"N/A"` and `"Not Assessed Yet"`.
5. **`client.py:get_class_grades`**:
   - Scrapes `has_submit_button` strictly by searching for `/dropbox` in `href`, missing ManageBac's modern `<a class="btn btn-primary">Submit Coursework</a>` buttons. As a result, `has_submit_button` was `False` for 100% of crawled tasks.
6. **`client.py:crawl_all`**:
   - Forces `"status": "submitted" if is_submitted else "not-submitted"`, arbitrarily assigning `"not-submitted"` to tasks that don't accept submissions (e.g. in-class quizzes).
7. **`filters.py:classify_task_view`**:
   - Bounded to `is_task_unfinished`. Because `is_task_unfinished` always returned `False`, unsubmitted overdue assignments were dumped into `past` instead of `overdue`.

---

## 3. Architecture & Design

### 3.1 Architecture Overview

We introduce a dedicated domain module: `src/mb_cli/task_status.py`.

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 Raw Scraped Task Data                  │
                  │   grade_letter, grade_score, labels, status, buttons   │
                  └──────────────────────────┬─────────────────────────────┘
                                             │
                                             ▼
                  ┌────────────────────────────────────────────────────────┐
                  │            src/mb_cli/task_status.py                   │
                  │            (Single Source of Truth)                    │
                  ├────────────────────────────────────────────────────────┤
                  │ 1. SubmissionStatus: SUBMITTED | PENDING | NONE        │
                  │ 2. GradeStatus:      GRADED | NOT_ASSESSED | NA        │
                  │ 3. LifecycleStatus:  TODO | COMPLETED                  │
                  │ 4. Timeline View:    UPCOMING | OVERDUE | PAST         │
                  │ 5. Display Strings:  grade display & status display    │
                  └──────────────┬──────────────────────────┬──────────────┘
                                 │                          │
                 ┌───────────────▼────────┐        ┌────────▼──────────────┐
                 │       Formatters       │        │     Filters & CLI     │
                 │   (mb list, mb view,   │        │  (--todo, --completed,│
                 │   mb grades)           │        │   --graded, daemon)   │
                 └────────────────────────┘        └───────────────────────┘
```

### 3.2 Domain Model (`src/mb_cli/task_status.py`)

#### Enums
```python
class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"  # Student has submitted coursework
    PENDING = "pending"      # Coursework submission is open/expected, but not yet submitted
    NONE = "none"            # No online submission entrance (offline/in-class quiz/task)

class GradeStatus(str, Enum):
    GRADED = "graded"                # Evaluated with real score or passing letter
    NOT_ASSESSED = "not_assessed"    # Waiting for evaluation ("Not Assessed Yet" or empty)
    NOT_APPLICABLE = "not_applicable"# Explicitly "N/A"
```

#### Core Evaluators

1. **`get_submission_status(task: dict) -> SubmissionStatus`**:
   - Returns `SUBMITTED` if:
     - `task.get("status") == "submitted"`
     - Any label/badge contains `"submitted"` (case-insensitive, without "not"/"un")
     - `detail.get("submission")` or `detail.get("submissions")` is non-empty.
   - Returns `PENDING` if:
     - Not submitted, AND
     - `task.get("has_submit_button") is True` OR `task.get("status") == "not-submitted"`.
   - Otherwise returns `NONE`.

2. **`get_grade_status(task: dict) -> GradeStatus`**:
   - Inspects `grade_letter` and `grade_score` (checking task and detail).
   - If letter is `"n/a"` or `"not applicable"` &rarr; `NOT_APPLICABLE`.
   - If letter is `"not assessed yet"` or `"not assessed"` &rarr; `NOT_ASSESSED`.
   - If score is empty or starts with `"0 /"` (placeholder) and has no letter &rarr; `NOT_ASSESSED`.
   - If valid score (e.g. `100 / 100 pts`) or letter (e.g. `A`, `B-`, `Complete`) &rarr; `GRADED`.
   - Otherwise &rarr; `NOT_ASSESSED`.

3. **`is_task_todo(task: dict) -> bool`**:
   - A task is a **TODO** if and only if:
     - It is NOT submitted (`submission_status != SUBMITTED`),
     - AND it is NOT graded (`grade_status != GRADED`),
     - AND (`submission_status == PENDING` OR it is an active upcoming assignment without a completed grade).
   - This guarantees:
     $$\text{Task is Todo} \iff \text{Shows as "Unsubmitted" in CLI} \iff \text{Matched by } \texttt{--todo}$$

4. **`is_task_completed(task: dict) -> bool`**:
   - `not is_task_todo(task)`. (Task is completed if it was submitted OR graded).

5. **`classify_task_view(task: dict, now_ref: datetime | None = None) -> str`**:
   - If `due_date > now`: `"upcoming"`.
   - If `due_date <= now`:
     - If `is_task_todo(task)`: `"overdue"`.
     - If `is_task_completed(task)`: `"past"`.

6. **`get_task_display_grade(task: dict, now_ref: datetime | None = None) -> str`**:
   - If `GradeStatus == GRADED`: returns formatted grade (e.g. `"A ( 100 / 100 pts )"`, `"100 / 100 pts"`, or `"A"`).
   - If `GradeStatus == NOT_APPLICABLE`: returns `"N/A"`.
   - If `GradeStatus == NOT_ASSESSED`:
     - If `is_task_submitted(task)`: returns `"Ungraded"`.
     - Else (unsubmitted):
       - If `due_date < now`: returns `"⚠ Unsubmitted"`.
       - Else: returns `"Unsubmitted"`.

7. **`get_task_display_status(task: dict) -> str`**:
   - If `is_task_todo(task)`: `"Incomplete (Todo)"`.
   - Else if `is_task_submitted(task)`: `"Complete (Submitted)"`.
   - Else if `get_grade_status(task) == GradeStatus.GRADED`: `"Complete (Graded)"`.
   - Else: `"Complete"`.

---

## 4. Ingestion Fixes (`src/mb_cli/client.py`)

1. **Submit Button Detection**:
   In `client.py:get_class_grades` and `get_task_detail`:
   ```python
   has_submit_btn = bool(
       card.find("a", href=re.compile(r"/core_tasks/\d+/dropbox"))
       or card.find(lambda el: el.name in ("a", "button") and any(
           kw in el.get_text().lower() for kw in ("submit coursework", "upload submission", "submit")
       ))
   )
   ```
2. **Preserve Scraped Status**:
   In `client.py:crawl_all`:
   Do not force all unsubmitted tasks to `"status": "not-submitted"`.
   ```python
   if is_submitted:
       status = "submitted"
   elif has_submit_btn or t.get("status") == "not-submitted":
       status = "not-submitted"
   else:
       status = t.get("status")
   ```

---

## 5. Consumer Refactoring

1. **`src/mb_cli/formatters.py`**:
   - `cmd_list`: replace local `get_grade_display` with `get_task_display_grade`.
   - `cmd_view`: use `get_task_display_grade` for grade, and `get_task_display_status` for status.
   - `cmd_grades` & `cmd_grades_all`: use `get_task_display_grade`.
2. **`src/mb_cli/filters.py`**:
   - Re-export `is_task_submitted`, `is_task_completed`, `classify_task_view`, and alias `is_task_unfinished = is_task_todo` to preserve full backward compatibility for any existing code or tests.
   - Update `matches_graded(task, graded)`: check `get_grade_status(task) == GradeStatus.GRADED`.
   - Update `matches_completed(task, completed)`: check `is_task_completed(task) == completed`.
3. **`src/mb_cli/daemon/`**:
   - Use `is_task_submitted(task)` and `is_task_todo(task)` in `service.py` and `scheduler.py`.

---

## 6. Verification & Test Plan

1. **Unit Tests for Domain Model (`tests/test_task_status.py`)**:
   - Test all combinations of (submitted, pending submission, offline quiz, graded, ungraded, not assessed, N/A, overdue, upcoming).
   - Verify invariant: `get_task_display_grade(t) in ("Unsubmitted", "⚠ Unsubmitted") <==> is_task_todo(t) is True`.
2. **Regression Testing**:
   - Existing tests in `tests/test_filters.py`, `tests/test_formatters.py`, `tests/test_client.py`, `tests/test_daemon*.py`.
3. **Real Snapshot Verification**:
   - Verify with the user's cached snapshot that `mb list --todo` returns the 8 unsubmitted upcoming assignments and correctly places overdue assignments into `overdue`.
