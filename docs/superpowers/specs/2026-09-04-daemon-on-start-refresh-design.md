# Daemon On-Start Refresh Lifecycle Callback Design

**Date:** 2026-09-04  
**Status:** Approved  
**Author:** Allen Sun & Antigravity  

---

## 1. Objective

Ensure that when the ManageBac background daemon restarts after a period of downtime, it does not operate on stale task data or miss alerts for newly published or modified assignments. The system introduces an `on_start` lifecycle callback hook on `DaemonService` that executes immediately on boot, defaulting to a full refresh of upcoming and unfinished tasks via `sync_upcoming_tasks()`.

---

## 2. Background & Problem Statement

In prior versions, `DaemonService.start()` contained the following optimization:
```python
if not self.state_manager.tasks_cache:
    self.sync_upcoming_tasks()
else:
    log.info("Loaded %d active tasks from state cache — skipping initial full crawl", len(self.state_manager.tasks_cache))
    self._last_full_sync = time.time()
```
Because `DaemonStateManager` persists `tasks_cache` across process restarts in `daemon_state.json`, `self.state_manager.tasks_cache` is non-empty upon daemon restart. Consequently:
1. Initial task synchronization was skipped entirely.
2. Any new assignments posted while the daemon was stopped were not ingested into `tasks_cache`.
3. Deadlines and submission statuses of unfinished tasks were not updated until the 12-hour fallback interval elapsed.
4. If a task's milestone was passed during downtime and the deadline remained in the future, the milestone reminder evaluation was delayed or computed with outdated task metadata.

---

## 3. Architecture & Design

### 3.1 `on_start` Lifecycle Callback Interface

A new parameter `on_start: Callable[[DaemonService], None] | None` is added to `DaemonService.__init__`:

```python
class DaemonService:
    def __init__(
        self,
        client: ManageBacClient,
        config: DaemonConfig | None = None,
        state_manager: DaemonStateManager | None = None,
        provider: AbstractNotificationProvider | None = None,
        auth_refresh_fn: Callable[[], bool] | None = None,
        on_start: Callable[[DaemonService], None] | None = None,
    ):
        ...
        self.on_start = on_start or (lambda svc: svc.sync_upcoming_tasks())
```

### 3.2 Boot Sequence in `DaemonService.start()`

The startup sequence in `start()` is updated as follows:

1. **Signal Handlers & Provider Boot**:
   - Register `SIGINT` and `SIGTERM` graceful shutdown handlers.
   - Start the event provider via `self.provider.start()`.
2. **Execute On-Start Callback**:
   - Log the execution of the startup callback.
   - Wrap the callback execution in a `try...except` block:
     ```python
     log.info("Executing daemon on-start callback...")
     try:
         self.on_start(self)
     except Exception as exc:
         log.warning("Daemon on-start callback encountered error: %s", exc)
     ```
   - If an exception occurs, it is logged, and the daemon proceeds with cached tasks to prevent complete service termination.
3. **Event Loop**:
   - Enter the main polling loop (`run_check_cycle()`).

### 3.3 Data Flow & Reconciliation via `sync_upcoming_tasks()`

When the default callback runs `svc.sync_upcoming_tasks()`:
1. **Fetch Upcoming Tasks**:
   - Queries `client.get_tasks_by_view("upcoming", max_pages=3)`.
   - Uses `auth_refresh_fn` to auto-relogin if session expiration occurs.
2. **Reconciliation**:
   - For each returned task `t`:
     - If the task is newly discovered (`is_new`), update `tasks_cache` and call `_suppress_past_milestones(t)` so past milestones are not erroneously fired.
     - If the task is already in `tasks_cache`, update with latest `due_date`, `status`, and metadata.
3. **State Maintenance**:
   - Sets `self._last_full_sync = time.time()`.
   - Prunes tasks whose deadlines expired more than 14 days ago (`state_manager.prune_old_tasks()`).
   - Atomically persists the updated state to `daemon_state.json`.

---

## 4. Error Handling & Edge Cases

| Scenario | Handling |
|---|---|
| Network error or session expiration during `on_start` | `sync_upcoming_tasks()` triggers `auth_refresh_fn` to re-authenticate. If still failing, logs warning and proceeds with cached data. |
| Custom `on_start` callback raises an exception | Exception is caught inside `start()`, logged as warning, allowing daemon to continue running. |
| Tasks submitted during downtime | On-start sync updates task `status` to `submitted` (or removes from upcoming), avoiding false reminder triggers. |
| No active internet connection on boot | Warning logged; daemon remains active and attempts check cycles when connection restores. |

---

## 5. Testing & Verification

1. **Unit Tests (`tests/test_daemon_service.py`)**:
   - `test_daemon_service_on_start_default`: Verify that by default, calling `service.start()` invokes `sync_upcoming_tasks()` even when `tasks_cache` contains entries.
   - `test_daemon_service_on_start_custom_callback`: Verify a custom callback function receives the `service` instance and is called during startup.
   - `test_daemon_service_on_start_error_resilience`: Verify that an exception in `on_start` is caught and logged without terminating startup.
2. **Full Test Suite**:
   - Run `pytest` across all daemon and CLI tests to ensure zero regressions.
