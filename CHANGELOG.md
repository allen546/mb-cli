# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release dates below are derived from git history (the last commit carrying each
version string in `pyproject.toml`); there are no git tags in this repository.

## [Unreleased]

### Fixed
- **Webhook URLs are no longer logged verbatim.** `log.info`/`log.warning`/
  `log.error` on every delivery and retry printed the full configured URL, so
  providers that carry the credential in the path or query (Slack
  `hooks.slack.com/services/T…/B…/<token>`, Bark `api.day.app/<key>/…`, WeCom
  `…/send?key=…`) wrote a live token into `daemon.log`. URLs are now redacted
  before logging — scheme, host, port and path shape are kept, credential-
  bearing path segments and query values are masked, and any `user:password@`
  userinfo is dropped. `daemon test-webhook` output additionally carries a
  `url_display` field.
- **The webhook retry "hard ceiling" now bounds request time, not just
  sleeps.** `MAX_TOTAL_RETRY_SECONDS` was checked only before `time.sleep`, so
  each attempt's `requests.post(timeout=10)` was unbounded by it and the final
  attempt always ran its full timeout. With the default `max_retries=3` one
  hanging endpoint cost ~33s of blocked polling, and the cost was linear in the
  number of configured endpoints (2 endpoints ≈ 66s against a 30s poll
  interval). Each attempt's timeout is now clamped to the remaining budget and
  the backoff sleep is clamped to it too, so one event can never exceed the
  ceiling.
- **Permanent 4xx are no longer retried.** 400/401/403/404/410/422 were retried
  three times with backoff even though no backoff can fix them, which both
  stalled the poll loop and delayed the diagnosis. Only genuinely transient
  statuses are retried now: 5xx, 408, 429 (and 425).
- **Webhook redirects are no longer followed.** `requests.post` followed
  redirects with the signed body and the signature headers intact, so a 307
  re-sent them to a *different* host and a 301/302 could downgrade https to
  http — defeating the point of signing. Dispatch now passes
  `allow_redirects=False`; a 3xx is reported as a permanent failure naming the
  `Location` so the configured URL can be corrected.

### Changed
- **BREAKING PROTOCOL CHANGE — webhook signatures.** `X-MB-Signature` now covers
  `X-MB-Timestamp` as well as the body:
  `sha256=` + HMAC-SHA256(secret, `f"{X-MB-Timestamp}.".encode() + body`). It
  previously covered the body alone, which left `X-MB-Timestamp`
  unauthenticated — anyone who captured a single POST could replay it
  indefinitely by rewriting that header, because the original digest still
  validated and the receiver's freshness check (`MAX_TIMESTAMP_SKEW_SECONDS`)
  waved the replay through. **Any deployed receiver rejects every payload until
  it adds the timestamp to its signed material.** `extras/mb-notifier/bark_webhook_receiver.py`
  and the FastAPI recipe in `docs/events.md` are updated in the same change;
  `verify_signature` now fails closed on a missing `X-MB-Timestamp` and checks
  the digest before freshness, so a restamped payload reports
  `signature_mismatch` rather than merely `stale_timestamp`.

### Added
- `CHANGELOG.md`, `SECURITY.md`, and GitHub Actions CI (`.github/workflows/ci.yml`).
- `[dependency-groups]` `dev` group in `pyproject.toml` declaring the test
  dependencies (`pytest`, `requests-mock`, `mcp`) that the suite always needed
  but that were never declared, so a plain `uv sync` could not run the tests.

### Fixed
- **Packaging:** the sdist no longer leaks a nested copy of the repository. It
  previously shipped 76 entries under `.claude/`, including
  `.claude/worktrees/finish-security-audit/` — a complete clone of the repo with
  its own `.git` (~75 MB). Fixed in both belts: `.claude/` added to
  `.gitignore`, and an explicit `[tool.hatch.build.targets.sdist]` include/exclude
  list in `pyproject.toml` that pins exactly what ships. The sdist went from 153
  entries to 63 files; no `.pyc`, no `.git`, no `.venv`, and no
  `docs/superpowers/` internal design docs ship.
- `mb --config` / `--session-file` help text no longer claims the format is
  TOML; it is JSON (`config.json` / `session.json`).
- `docs/library.md` no longer points at a non-existent
  `~/.config/mb-crawler/config.toml`.
- **The `mcp` extra is now bounded (`mcp>=1.20,<2`).** It was unbounded, so a
  fresh resolve installed mcp 2.x, which removed `mcp.server.fastmcp` — every
  `mb-mcp` invocation died with `ModuleNotFoundError` on import, even when the
  extra was installed correctly.

### Changed
- README Installation now documents the `mcp` extra
  (`pip install "mb-cli[mcp]"`) — the documented `mb-mcp` command imports `mcp`
  unguarded and failed with `ModuleNotFoundError` on a plain install.
- README now documents `mb daemon start -b` / `--background`. `daemon start`
  runs in the **foreground** by default and dies with the terminal; only `-b`
  detaches it. `--pid-file` / `--log-file` documented per subcommand.
- README now documents the previously undocumented `mb submissions`,
  `mb download`, `mb feedback`, and `mb daemon install` / `uninstall` /
  `configure-channel` subcommands.
- README now documents the `MB_CRAWLER_PASSWORD` and `MB_CRAWLER_COOKIE`
  environment variables alongside `MB_WEBHOOK_SECRET`.
- README now flags the `--poll-interval` (`mb daemon run`) vs `--interval`
  (`mb daemon start`) flag-name asymmetry, and qualifies the claim that
  `--config` / `--session-file` are universal — the daemon subcommands expose
  only `--daemon-config` and/or `--pid-file` / `--log-file`, varying by
  subcommand.

## [0.3.0] - 2026-09-17

### Added
- Complete library + downstream security audit.
- Comprehensive Python SDK and library reference (`docs/library.md`).

### Changed
- Bumped version to 0.3.0 and documented remote separated deployment.
- `ManageBacClient.from_config()` implemented; `docs/events.md` updated.

### Fixed
- CLI command flags, the systemd unit, and Python SDK snippets corrected in the
  README and the notifier guide.
- `student_name` fallback polished and covered by a test for the default
  `from_config` argument.

## [0.2.5] - 2026-09-14

### Added
- Real-time event streaming: `ManageBacDaemon.stream()` async event generator.
- Comprehensive event stream specification and integration guide (`docs/events.md`).
- Design specs and implementation plan for modular event stream and notifier
  separation.

### Changed
- Standardized the `MBEvent` payload schema.
- Extracted the Bark webhook receiver and personal config into
  `extras/mb-notifier`.
- Refreshed the README and added the downstream notifier guide.

### Fixed
- Stream lifecycle and queue-safety issues raised in review.
- Defensive fallback URL resolution in `service.py`.

## [0.2.4] - 2026-09-13

### Added
- `mb submissions` CLI for submission lifecycle management (`--list`, `--add`,
  `--delete`, `--check-feedback`), with a matching MCP tool.

### Changed
- Bumped version; grade-release alert suppression and webhook improvements.

## [0.2.3] - 2026-09-05

### Added
- Unified task status and lifecycle domain model.
- Teacher feedback fetching, including support for modern task detail page
  submissions and the PSPDFKit preview modal.
- `on_start` lifecycle callback for a full upcoming-task refresh.
- Just-in-time submission verification, eliminating unnecessary full recrawls.
- Lightweight Bark webhook receiver adapter.
- Real-time notification daemon and webhook engine with the MNN Hub provider and
  a DDL scheduler, plus a complete E2E integration test suite.
- Compact 3-field notification layout with exact course alias resolution.
- Design specs for the daemon, the compact Bark layout, the on-start refresh
  callback, unified task status, and submissions lifecycle management.

### Fixed
- Daemon no longer notifies for submitted or graded tasks.
- `_check_is_task_submitted` now checks the task page Submitted badge.
- Past milestones suppressed on task discovery; DDL reminders formatted with the
  exact remaining time.
- `new_task` event mapped; class and task names cleaned; richer 4-line Bark
  notification.
- Bark notification format enforced as a compact 4-line layout bounded to the
  date line width.
- `run_forever` alias restored on `DaemonService`.
- Resolved code review issues in the daemon.

### Changed
- Deduplicated task URL parsing (`parse_task_url`), task classification logic
  across main/client/formatters, snapshot IO and diffing in the daemon package,
  and submission/completion/classification logic in `filters.py`; the stealth
  crawler now reuses `client.get_submissions`.
- `.worktrees/` ignored; `course_aliases.json` ignore scoped to root level.

## [0.2.2] - 2026-06-13

### Added
- Auth health check and silent re-login on an expired cookie.
- `load_creds()` for external credential files.

### Fixed
- Cache namespaced by email hash; `list_tasks` crawl optimized.
- `submit_file` task ID resolution optimized.
- Cache no longer sleeps on cache hits inside crawler loops.
- Hub jitter adjusted to 1–3s for reads and removed from mutations.

## [0.2.0] - 2026-04-30

### Added
- Stealth daemon with active windows and index-only diffing.
- Daemon active hours and randomized polling interval.
- `channel_send` delivery mode.
- Remember-me login support (30-day sessions by default).
- Referer header and random jitter between requests.

### Fixed
- Pagination fixed; retry with backoff and grade frequency counting added.

## [0.1.0] - 2026-04-29

### Added
- Initial release: `mb` CLI, `ManageBacClient` SDK, disk-based response cache
  with configurable TTL.
- Status and grade filters, auto-aggregated grades, task grade parsing.
- Pretty-printed task listing table with CJK/double-width alignment padding.
- `mb list --tag / -t` filtering and tag filtering in the MCP `list_tasks` tool.
- `mb list --deleted` to include tasks deleted from the server; instant
  snapshot caching; grade-status rendering (Complete / Incomplete) and a
  combined letter + score grade column.
- Pretty table formatter for `count-grade-freq`; attachment downloads;
  student name captured and updated on dashboard loads.
- Legal disclaimer regarding the Faria/ManageBac terms of service.
- Project renamed from `mb-crawler` to `mb-cli` (command: `mb`).

[Unreleased]: https://github.com/allen/mb-crawler/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/allen/mb-crawler/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/allen/mb-crawler/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/allen/mb-crawler/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/allen/mb-crawler/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/allen/mb-crawler/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/allen/mb-crawler/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/allen/mb-crawler/releases/tag/v0.1.0
