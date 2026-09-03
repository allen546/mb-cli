# Code Duplication & Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate redundant task classification, URL parsing, dropbox scraping, and snapshot diffing logic across the codebase to ensure consistency and single-source-of-truth maintainability.

**Architecture:** Centralize task status/completion/view evaluation in `src/mb_cli/filters.py`, URL and identifier resolution in `src/mb_cli/client.py`, reuse existing client dropbox inspection in `src/mb_cli/daemon/stealth.py`, and consolidate snapshot loading/diffing between `src/mb_cli/__main__.py` and `src/mb_cli/daemon/__init__.py`.

**Tech Stack:** Python 3.14, BeautifulSoup4, pytest

---

### Task 1: Centralize Task Evaluation in `src/mb_cli/filters.py`

**Files:**
- Modify: `src/mb_cli/filters.py:30-185`
- Test: `tests/test_filters.py`

- [ ] **Step 1: Write failing tests for canonical task evaluation helpers**

Add tests for `is_submitted_badge`, `is_task_submitted`, `is_task_unfinished`, `is_task_completed`, and `classify_task_view` to `tests/test_filters.py`:

```python
def test_is_submitted_badge():
    from mb_cli.filters import is_submitted_badge
    assert is_submitted_badge("Submitted") is True
    assert is_submitted_badge("submitted") is True
    assert is_submitted_badge("Not Submitted") is False
    assert is_submitted_badge("unsubmitted") is False
    assert is_submitted_badge("Pending") is False


def test_is_task_submitted():
    from mb_cli.filters import is_task_submitted
    assert is_task_submitted({"status": "submitted"}) is True
    assert is_task_submitted({"labels": ["Submitted"]}) is True
    assert is_task_submitted({"labels": ["Not Submitted"]}) is False
    assert is_task_submitted({"detail": {"submission": {"id": 123}}}) is True
    assert is_task_submitted({}) is False


def test_is_task_unfinished_and_completed():
    from mb_cli.filters import is_task_completed, is_task_unfinished

    # Incomplete task: has submit button, not submitted, no passing grade, assessed
    task_todo = {
        "has_submit_button": True,
        "status": "not-submitted",
        "grade_letter": None,
        "grade_score": None,
        "labels": [],
    }
    assert is_task_unfinished(task_todo) is True
    assert is_task_completed(task_todo) is False

    # Completed: submitted
    task_submitted = dict(task_todo, status="submitted")
    assert is_task_unfinished(task_submitted) is False
    assert is_task_completed(task_submitted) is True

    # Completed: has valid grade letter
    task_graded = dict(task_todo, grade_letter="A")
    assert is_task_unfinished(task_graded) is False
    assert is_task_completed(task_graded) is True

    # Completed: not assessed yet (exempt)
    task_exempt = dict(task_todo, labels=["Not Assessed Yet"])
    assert is_task_unfinished(task_exempt) is False
    assert is_task_completed(task_exempt) is True


def test_classify_task_view():
    from datetime import datetime, timedelta
    from mb_cli.filters import classify_task_view

    now = datetime(2026, 9, 10, 12, 0, 0)

    # Future task -> upcoming
    t_future = {
        "due_date": "2026-09-15 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_future, now_ref=now) == "upcoming"

    # Past deadline, unfinished -> overdue
    t_overdue = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "not-submitted",
    }
    assert classify_task_view(t_overdue, now_ref=now) == "overdue"

    # Past deadline, completed -> past
    t_past = {
        "due_date": "2026-09-05 12:00:00",
        "has_submit_button": True,
        "status": "submitted",
    }
    assert classify_task_view(t_past, now_ref=now) == "past"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_filters.py -k "test_is_submitted_badge" -v`
Expected: FAIL with `ImportError: cannot import name 'is_submitted_badge' from 'mb_cli.filters'`

- [ ] **Step 3: Implement canonical helpers in `src/mb_cli/filters.py`**

Add canonical functions to `src/mb_cli/filters.py` and fix `matches_submitted()`:

```python
def is_submitted_badge(badge: str) -> bool:
    """Return True if badge indicates submission without negative qualifiers."""
    b = badge.strip().lower()
    return "submitted" in b and "not" not in b and "un" not in b


def is_task_submitted(task: dict) -> bool:
    """Return True if task has been submitted based on status, labels, or details."""
    status = str(task.get("status") or "").lower()
    if status == "submitted":
        return True

    labels = task.get("labels") or []
    if any(is_submitted_badge(l) for l in labels):
        return True

    detail = task.get("detail") or {}
    if str(detail.get("status") or "").lower() == "submitted":
        return True
    if any(is_submitted_badge(l) for l in (detail.get("labels") or [])):
        return True
    if detail.get("submission") or detail.get("submissions"):
        return True

    return False


def is_task_unfinished(task: dict) -> bool:
    """Return True if a task requires submission and is uncompleted."""
    if is_task_submitted(task):
        return False

    labels = (task.get("labels") or []) + ((task.get("detail") or {}).get("labels") or [])
    labels_lower = [l.lower() for l in labels]

    grade_letter = task.get("grade_letter") or (task.get("detail") or {}).get("grade_letter")
    grade_score = task.get("grade_score") or (task.get("detail") or {}).get("grade_score")

    is_zero_score = False
    if grade_score and re.match(r"^\s*0\s*/", grade_score):
        is_zero_score = True

    has_score = bool(grade_score and grade_score.strip() and grade_score.strip() != "-")
    has_letter = bool(grade_letter and grade_letter.strip())
    has_completed_grade = (has_score or has_letter) and not is_zero_score

    is_not_assessed = "not assessed yet" in labels_lower or (
        bool(grade_letter) and "not assessed" in grade_letter.lower()
    )
    has_submit_btn = bool(
        task.get("has_submit_button", False)
        or (task.get("detail") or {}).get("has_submit_button", False)
    )

    return has_submit_btn and not has_completed_grade and not is_not_assessed


def is_task_completed(task: dict) -> bool:
    """Return True if a task is not unfinished."""
    return not is_task_unfinished(task)


def classify_task_view(task: dict, now_ref: datetime | None = None) -> str:
    """Classify a task into 'upcoming', 'overdue', or 'past' based on due date and status."""
    from .client import parse_due_date

    now = now_ref or datetime.now()
    due_date = task.get("due_date")
    due_dt = parse_due_date(due_date, now_ref=now) if due_date else None

    if due_dt and due_dt > now:
        return "upcoming"
    if is_task_unfinished(task):
        return "overdue"
    return "past"
```

Update `matches_submitted()` and `matches_completed()` in `src/mb_cli/filters.py`:

```python
def matches_submitted(task: dict, submitted: bool) -> bool:
    """Return True if the task's submission state matches the query."""
    return is_task_submitted(task) == submitted


def matches_completed(task: dict, completed: bool) -> bool:
    """Return True if the task's completion state matches the query."""
    return is_task_completed(task) == completed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_filters.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mb_cli/filters.py tests/test_filters.py
git commit -m "refactor: centralize task submission, completion and classification logic in filters.py"
```

---

### Task 2: Deduplicate Task Classification in `__main__.py`, `client.py`, and `formatters.py`

**Files:**
- Modify: `src/mb_cli/__main__.py:165-210`
- Modify: `src/mb_cli/client.py:1280-1335`
- Modify: `src/mb_cli/formatters.py:175-207`
- Test: `tests/test_main.py`
- Test: `tests/test_formatters.py`

- [ ] **Step 1: Write regression test verifying unified classification**

Ensure existing snapshot merging and formatters maintain identical output:

```python
def test_reclassify_tasks_uses_canonical_classifier(tmp_path: Path):
    from datetime import datetime
    from mb_cli.__main__ import _reclassify_tasks
    now = datetime(2026, 9, 10, 12, 0, 0)
    merged_map = {
        "1": {"id": "1", "due_date": "2026-09-20 12:00:00", "has_submit_button": True, "status": "not-submitted"},
        "2": {"id": "2", "due_date": "2026-09-01 12:00:00", "has_submit_button": True, "status": "not-submitted"},
        "3": {"id": "3", "due_date": "2026-09-01 12:00:00", "has_submit_button": True, "status": "submitted"},
    }
    res = _reclassify_tasks(merged_map, now_ref=now)
    assert [t["id"] for t in res["upcoming"]] == ["1"]
    assert [t["id"] for t in res["overdue"]] == ["2"]
    assert [t["id"] for t in res["past"]] == ["3"]
```

- [ ] **Step 2: Replace duplicated block in `src/mb_cli/__main__.py`**

In `src/mb_cli/__main__.py`:
Extract `_reclassify_tasks(merged_map, now_ref)` using `classify_task_view`:

```python
    for t in merged_map.values():
        view = classify_task_view(t, now_ref=now_ref)
        t["view"] = view
        if view == "upcoming":
            upcoming.append(t)
        elif view == "overdue":
            overdue.append(t)
        else:
            past.append(t)
```

- [ ] **Step 3: Replace duplicated block in `src/mb_cli/client.py`**

In `src/mb_cli/client.py:1283-1331` (in `crawl_all`):
Replace the 30-line duplicate block with calls to `is_task_submitted` and `classify_task_view`:

```python
    is_submitted = is_task_submitted(t)
    reconstructed_task = {
        "id": task_id,
        "title": t.get("title"),
        "class_name": class_name,
        "due_date": due_date,
        "link": t.get("url"),
        "grade_letter": grade_letter,
        "grade_score": t.get("points"),
        "labels": labels or None,
        "status": "submitted" if is_submitted else "not-submitted",
        "has_submit_button": bool(t.get("has_submit_button", False)),
    }
    view = classify_task_view(reconstructed_task)
    reconstructed_task["view"] = view
    if view == "upcoming":
        upcoming.append(reconstructed_task)
    elif view == "overdue":
        overdue.append(reconstructed_task)
    else:
        past.append(reconstructed_task)
```

- [ ] **Step 4: Replace duplicated block in `src/mb_cli/formatters.py`**

In `src/mb_cli/formatters.py:177-207`:
Replace duplicate manual checks with:

```python
    from .filters import is_task_submitted, is_task_unfinished

    is_submitted = is_task_submitted(task) or (detail and is_task_submitted(detail))
    is_unfinished = is_task_unfinished(task)

    if is_unfinished:
        status_display = "Incomplete (Todo)"
    else:
        status_display = "Complete"
        if is_submitted:
            status_display += " (Submitted)"
        elif is_not_assessed:
            status_display += " (Not Assessed Yet)"
```

- [ ] **Step 5: Run tests to verify all suites pass**

Run: `./.venv/bin/pytest tests/test_main.py tests/test_client.py tests/test_formatters.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/mb_cli/__main__.py src/mb_cli/client.py src/mb_cli/formatters.py
git commit -m "refactor: eliminate duplicated task classification logic across main, client, and formatters"
```

---

### Task 3: Unify URL and Identifier Parsing (`parse_task_url`)

**Files:**
- Modify: `src/mb_cli/client.py:40-60`
- Modify: `src/mb_cli/__main__.py:621-643`
- Modify: `src/mb_cli/mcp_server.py:235-265`
- Modify: `src/mb_cli/daemon/provider.py:155-165`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write unit test for `parse_task_url`**

In `tests/test_client.py`:

```python
def test_parse_task_url():
    from mb_cli.client import parse_task_url
    assert parse_task_url("https://school.managebac.cn/student/classes/1000012/core_tasks/1000099") == ("1000012", "1000099")
    assert parse_task_url("/student/classes/1000012/core_tasks/1000099/dropbox") == ("1000012", "1000099")
    assert parse_task_url("1000099") == (None, "1000099")
    assert parse_task_url("invalid-url") == (None, "invalid-url")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_client.py -k "test_parse_task_url" -v`
Expected: FAIL with `ImportError: cannot import name 'parse_task_url'`

- [ ] **Step 3: Implement `parse_task_url` in `src/mb_cli/client.py`**

Add to `src/mb_cli/client.py`:

```python
def parse_task_url(target: str) -> tuple[str | None, str | None]:
    """Extract (class_id, task_id) from a ManageBac URL or task identifier string."""
    if not target:
        return None, None
    m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", target)
    if m:
        return m.group(1), m.group(2)
    clean = target.rstrip("/").split("/")[-1]
    return None, clean if clean else None
```

- [ ] **Step 4: Adopt `parse_task_url` across consumers**

- In `src/mb_cli/__main__.py:_resolve_task_ids`:
  ```python
  cid, tid = parse_task_url(target)
  if cid and tid:
      return cid, tid
  task_id = tid or target
  ```
- In `src/mb_cli/mcp_server.py:submit_file`:
  ```python
  cid, tid = parse_task_url(task_id)
  if cid and tid:
      class_id = cid
  ```
- In `src/mb_cli/daemon/provider.py:normalize_notification`:
  ```python
  cid, tid = parse_task_url(href)
  if cid and tid:
      class_id, task_id = int(cid), int(tid)
  ```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_client.py tests/test_daemon_provider.py tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/mb_cli/client.py src/mb_cli/__main__.py src/mb_cli/mcp_server.py src/mb_cli/daemon/provider.py tests/test_client.py
git commit -m "refactor: unify task URL parsing with parse_task_url"
```

---

### Task 4: Deduplicate Dropbox Scraping in `StealthTaskCrawler`

**Files:**
- Modify: `src/mb_cli/daemon/stealth.py:80-115`
- Test: `tests/test_daemon_stealth.py`

- [ ] **Step 1: Write test verifying stealth crawler delegates dropbox checks**

Add to `tests/test_daemon_stealth.py`:

```python
def test_stealth_crawler_uses_client_get_submissions():
    mock_client = MagicMock()
    mock_client.base = "https://school.managebac.cn"
    sample_html = """
    <html><body>
      <h3 class="title">Essay</h3>
      <a href="/student/classes/101">Class</a>
      <a href="/student/classes/101/core_tasks/202/dropbox">Dropbox</a>
    </body></html>
    """
    mock_client._get.return_value = BeautifulSoup(sample_html, "html.parser")
    mock_client.get_submissions.return_value = [{"name": "essay.pdf", "url": "/attachments/1"}]

    crawler = StealthTaskCrawler(mock_client, StealthConfig(enabled=False))
    task = crawler.fetch_task_details(class_id=101, task_id=202)
    assert task["status"] == "submitted"
    mock_client.get_submissions.assert_called_once_with("101", "202")
```

- [ ] **Step 2: Update `src/mb_cli/daemon/stealth.py` to reuse `client.get_submissions` and `is_submitted_badge`**

Replace manual dropbox page parsing in `src/mb_cli/daemon/stealth.py`:

```python
        from ..filters import is_submitted_badge

        # Check for submitted indicators in badges
        if any(is_submitted_badge(b) for b in badges):
            status = "submitted"

        # If button exists and status is not yet marked submitted, check dropbox submissions
        if has_submit_btn and status != "submitted":
            try:
                submissions = self.client.get_submissions(class_id_str, task_id_str)
                if submissions and not submissions[0].get("error"):
                    status = "submitted"
            except Exception:
                pass
```

- [ ] **Step 3: Run tests to verify it passes**

Run: `./.venv/bin/pytest tests/test_daemon_stealth.py tests/test_daemon_e2e_integration.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/mb_cli/daemon/stealth.py tests/test_daemon_stealth.py
git commit -m "refactor: reuse client.get_submissions in stealth crawler"
```

---

### Task 5: Deduplicate Snapshot IO & Diffing in `src/mb_cli/daemon/__init__.py`

**Files:**
- Modify: `src/mb_cli/daemon/__init__.py:133-260`
- Modify: `src/mb_cli/__main__.py:85-105`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write test verifying canonical snapshot IO and diffing**

In `tests/test_daemon.py`:

```python
def test_canonical_snapshot_io_and_diff(tmp_path: Path):
    from mb_cli.__main__ import load_snapshot, save_snapshot
    from mb_cli.daemon import diff_index

    snap_file = tmp_path / "snap.json"
    save_snapshot(snap_file, {"upcoming": [{"id": "10", "title": "Math"}]})
    loaded = load_snapshot(snap_file)
    assert loaded["upcoming"][0]["id"] == "10"

    old = {"upcoming": []}
    new = {"upcoming": [{"id": "10", "title": "Math"}]}
    alerts, changed_ids = diff_index(old, new)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "new_upcoming"
    assert changed_ids == ["10"]
```

- [ ] **Step 2: Consolidate `_load_snapshot` / `_save_snapshot` and diff functions**

In `src/mb_cli/daemon/__init__.py`:
- Replace private `_load_snapshot` and `_save_snapshot` with imports from `..__main__ import load_snapshot, save_snapshot` (or shared module).
- Unify `_diff_snapshots_full` into `diff_index`: Make `_diff_snapshots_full` a thin wrapper around `diff_index(old, new)[0]` to maintain full backward compatibility while eliminating the duplicate diff loop logic.

- [ ] **Step 3: Run all daemon tests to verify no regressions**

Run: `./.venv/bin/pytest tests/test_daemon*.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/mb_cli/daemon/__init__.py tests/test_daemon.py
git commit -m "refactor: consolidate snapshot IO and diffing in daemon package"
```

---

### Task 6: Full Verification Run

**Files:**
- All tests across repository

- [ ] **Step 1: Run complete test suite**

Run: `./.venv/bin/pytest tests -v`
Expected: 372+ passed, 0 failures

- [ ] **Step 2: Commit final refactoring validation**

```bash
git commit --allow-empty -m "chore: verify complete test suite passing after code deduplication"
```
