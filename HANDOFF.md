# Handoff: `tahuti` — ManageBac toolkit

**Date:** 2026-09-19
**Branch:** `publish-prep` (24 commits ahead of the pre-work HEAD `2254a01`)
**State:** 679 tests passing on **both** macOS (arm64) and Linux (aarch64),
builds clean, `twine check` passes on both artifacts.
**Nothing has been pushed to GitHub, published, or released.**

> **This copy now lives on a Raspberry Pi** at `/mnt/pi-data/tahuti` (Linux
> 6.18 aarch64, Python 3.13.5, `uv` 0.12.16 installed). `origin` still points at
> the un-renamed `github.com/allen546/mb-cli`. Branches present locally:
> `publish-prep` (checked out), `main`, `docs-fixes`, `cli-rename`,
> `windows-support`, and `worktree-finish-security-audit` (the last one is
> preserved for reference only — it is an ancestor-less legacy clone whose
> content is already contained in `publish-prep`; its history is also bundled
> at `/mnt/pi-data/tahuti-imported/legacy-security-audit.bundle`).
>
> Files imported from the Mac that are not part of the repository live in
> `/mnt/pi-data/tahuti-imported/`, deliberately outside the checkout so
> `git status` stays clean. Nothing in there is tracked or committed. It holds
> the old `.claude/` scratch tree (with its original dotfile name), the loose
> scratch scripts that used to sit in the repo root (`backup.txt`,
> `course_aliases.json`, `fetch_attachments.py`, `freqs.txt`, `open_classes.py`,
> `test_login.py` — all gitignored by name, all personal data), and
> `legacy-security-audit.bundle`.
>
> Two of these hold personal data and are worth knowing about:
> `course_aliases.json` maps your real ManageBac class names to short aliases,
> and `backup.txt` is a grade dump. Neither should be committed or shipped.
>
> The Mac's original checkout at `~/Desktop/t8/mb-crawler` is unmodified and
> still on `main` at `2254a01`. It was not touched.

This document is for whoever continues the work — agent or human. Read
§1 before touching anything; §7 lists what is deliberately unfinished.

---

## 1. Read this first

### Identity decisions (settled — do not re-litigate without cause)

| Thing | Value | Why |
|---|---|---|
| PyPI / project name | `tahuti` | `mb-cli` was already taken on PyPI by an unrelated project |
| Console commands | `tahuti`, `tahuti-mcp` | Primary; consistent with the package name |
| Command aliases | `mb`, `mb-mcp` | Same entry points; kept so existing habits don't break |
| Python import path | `mb_cli` (**unchanged**) | Renaming it would break callers for zero benefit. Deliberate. |
| State directory | `~/.config/tahuti/` | Renamed from `mb-crawler`. No legacy fallback — there are no users yet. |
| Env vars | `MB_CRAWLER_*` (**unchanged**) | Renaming would break working setups; not part of command identity |
| Service labels | `com.tahuti.daemon`, `tahuti-daemon.service` | Renamed with the project |
| License | MIT, © 2026 Allen Sun | Consistent across `LICENSE`, `pyproject.toml`, wheel metadata |

`tahuti` is the Egyptian Ḏḥwtj, "He Who is Like the Ibis" — the scribe who
records, the arbiter who weighs, the messenger who carries word between
parties. That maps onto what the event engine actually does. The Greek name
`Thoth` is taken on PyPI; the Egyptian original was free on PyPI, npm and
conda-forge, with no meaningful GitHub collision.

This copy of the repository is running on a **Raspberry Pi (aarch64 Linux,
Python 3.13.5)**, checked out on branch **`publish-prep`**. `uv` 0.12.16 is
already installed at `~/.local/bin/uv` — if your `PATH` does not include it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### How to run the tests

```bash
cd ~/tahuti                       # or /mnt/pi-data/tahuti
uv sync --group dev               # creates .venv, installs pytest/requests-mock/mcp
uv run pytest -q -p no:cacheprovider
```

`PYTHONPATH=src` is only needed if you bypass `uv` and run the interpreter
directly — without it you can silently test some other checkout's code instead
of this one. Confirm with `uv run python -c "import mb_cli; print(mb_cli.__file__)"`:
it must resolve **inside this directory**.

**On the originating Mac, `uv run` and `uv sync` are broken** by a pre-existing
Rosetta/`cryptography` source-build error (the local Rust toolchain lacks the
`x86_64-apple-darwin` target). That is a Mac problem, not a project problem — do
not let it send you rewriting build config. `uv lock` and `uv build` do work
there. On this Linux box a normal `uv sync` is expected to work; if it does not,
fall back to `python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]' pytest
requests-mock` and run `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`.

---

## 2. Running end-to-end tests without exposing credentials

An agent must **never** be given the ManageBac password, and must never be the
one to type it. Enter it yourself, by hand, in a terminal you control. Three
options, best first:

**1. Keychain (recommended).** Login once yourself; afterwards `tahuti` and the
daemon both read the OS keychain, so nothing is on disk and no env var exists
for a process table to leak:

```bash
cd /mnt/pi-data/tahuti
uv run tahuti login --keychain     # you type the password at the prompt
```

On this host that is the Linux Secret Service via `secret-tool`. It needs a
running secret service — on a headless Pi, `gnome-keyring-daemon --unlock` may
have to be started in your session first. If it is unavailable, `login`
**falls back to `creds.json` at 0600 and warns** rather than silently losing the
credential; that fallback is intentional.

**2. Environment variable, entered by you, in the same shell invocation.** Fine
for one command, but a leaked env var is directly usable as a credential, and a
process's environment is readable by its own user:

```bash
read -rs MB_CRAWLER_PASSWORD && export MB_CRAWLER_PASSWORD
# paste, press Enter, then:
uv run tahuti list
```
Note `read -rs` keeps the secret out of your shell history — unlike
`tahuti login --password hunter2`, which lands in `~/.zsh_history`.

**3. Nothing at all.** `tahuti login --temp` writes nothing to disk
(`remember_me=0`), skips saving the session cookie, and disables the response
cache. Best when you only need a single session and care most about leaving no
trace.

Clean up afterwards with `uv run tahuti logout`, which deletes `creds.json`,
the keychain entry, the session cookie, and the response cache by default.

**Do not:** commit `creds.json`/`session.json` (they are gitignored, keep them
that way), pass the password as a CLI flag, or hand it to a subagent. Note that
`course_aliases.json` (in `/mnt/pi-data/tahuti-imported/`) maps your real class
names to short aliases and is personal data — gitignored, never committed, never
in the sdist. If you want the notifier to use it, copy it to
`~/.config/tahuti/course_aliases.json` and edit that, as its own header comment
says.

### 2.1 Why the Linux keychain does not work on this host

Established 2026-09-19 by direct experiment. Worth reading before anyone
"fixes" this, because the obvious diagnosis is wrong.

The symptom is:

```
secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY
```

That message names X11, which invites the conclusion that libsecret needs a
graphical session. **It does not.** The failure is in D-Bus *autolaunch*: with
`DBUS_SESSION_BUS_ADDRESS` unset, `secret-tool` tries to start a session bus
itself, and only that autolaunch path is X11-dependent. Supply a bus and D-Bus
is satisfied — `dbus-run-session` activates `org.freedesktop.secrets`
successfully with no display at all.

It then fails one layer deeper:

```
secret-tool: Cannot create an item in a locked collection
Gtk-WARNING: cannot open display
```

The secret service is up and talking. The **default collection is locked**, and
gnome-keyring unlocks it via `org.gnome.keyring.SystemPrompter` — a GTK dialog,
which cannot run headless and exits 1. Both documented headless workarounds
fail identically: `gnome-keyring-daemon --login` with a password on stdin, and
`--unlock` with a password on stdin.

The reason, read out of the keyring file itself: `~/.local/share/keyrings/
default` points at `Default_Keyring`, whose leading byte is neither `0x00`
(hashed credential — unlockable with a password) nor `0x01` (no credential). It
carries no password to check against, having been created by a GUI session that
no longer exists, so headless gnome-keyring has no way to unlock it by design.

**Therefore `login` correctly falls back to `creds.json` at 0600 and warns.**
That is the intended behavior, not a defect. The escape hatch — delete
`Default_Keyring` so a fresh password-less one is created — is deliberately
*not* recommended: it yields encryption-at-rest that is weaker than the 0600
file it replaces, behind a GTK dependency.

**Verified-working alternatives on this host, and why none was adopted:**

| Option | Present | Works unattended | Why not |
|---|---|---|---|
| gnome-keyring | yes | no | locked collection, GUI prompter |
| `systemd-creds` | yes | yes | **no TPM** (`/dev/tpm0`, `/dev/tpmrm0` absent) — nothing to seal to |
| `keyctl` kernel keyring | yes | no | session-scoped; dies with the session |
| `pass`/`gopass`/`age`/`sops` | no | yes | would add a runtime dep, ruled out by §7's stdlib-only rule |

The real constraint is not headlessness but **unattended operation**: the
daemon is a systemd service with nobody at a keyboard, so any store needing a
passphrase at unlock time is unusable for it. That is why `creds.json` is the
default and the keychain is opt-in. A genuine upgrade is `pass`/`age` with the
key supplied via systemd `LoadCredential` — but that is a *daemon* feature, not
a `login` feature, and a new dependency.

**What still needs doing:** run the Linux keychain path on a host with a real
graphical session (§6.5). `keychain.py`'s docstring also claims the Linux
helper is "Absent on a headless box with no secret service" — now known to be
wrong in a way that sends the reader chasing a dependency problem that does not
exist. Worth correcting to say the *unlock* is impossible, not the service.

---

## 3. Release gates — four separate gates, not one

This is the most important operational fact in this document.

| Gate | Unverified Windows code blocks it? |
|---|---|
| **Push to GitHub** | **No** — land it, let it be reviewed |
| **CI** | **Yes** — a Windows job would run unexercised code |
| **GitHub Releases** | **Yes** — that is a shipped artifact |
| **PyPI** | **Yes** — irreversible; names and versions are never reusable |

The asymmetry is deliberate. Code can reach `main` while still needing
verification; what must not happen is a *shipped* artifact containing a path
nobody has executed.

### Pre-release verification gate (owner-specified, three platforms)

1. **macOS** — the originating Mac. Full suite green there.
2. **Linux** — **done for the unit suite**: 679/679 pass on this Pi. Still
   outstanding is the daemon end-to-end (systemd install, not just launchd) —
   the unit tests cover launchd and systemd *file generation*, but no real
   service has been installed and started here yet. Also outstanding: the
   Linux keychain has never worked here, for a reason that is *not* a missing
   dependency — see §6.5.
3. **Windows laptop** — manual verification available. Specifically:
   - `tahuti login --keychain` → store → `tahuti logout` → confirm the
     Credential Locker entry is gone
   - silent re-login from the keychain (the daemon path)
   - `daemon install` — **currently unsupported on Windows** (§7)

---

## 4. What was done

### 4.1 Rename `mb-cli` → `tahuti`
Package name, description, keywords (`managebac` kept first for PyPI search
discoverability, since PyPI indexes summary + keywords and not just the name),
Python 3.10–3.14 classifiers, real `[project.urls]` (the old
`github.com/allen/mb-crawler` was a dead link). `uv.lock` regenerated.

### 4.2 Security
- `tahuti logout` now **deletes the stored password** by default
  (`--keep-credentials` opts back in). Previously it left `creds.json` behind.
- New `src/mb_cli/keychain.py` — opt-in OS keychain, **stdlib only, no new
  dependencies**: macOS `security`, Linux `secret-tool`, Windows
  `powershell.exe` + WinRT `PasswordVault`.
- Weak-permission warnings on startup for loose config files.
- `MB_CRAWLER_PASSWORD` / `MB_CRAWLER_COOKIE` were **write-only** (exported to
  the daemon child, never read back). Now genuinely read.
- `SECURITY.md` rewritten; several of its claims were factually wrong and were
  corrected against source (see §6).

### 4.3 Interface
Two genuine defects, both of which the owner's instinct about a "mostly
invalid interface" was pointing at:
- `mb download` called `print_payload(args.output, args.format)` on four error
  paths while its parser defined neither flag → `AttributeError` instead of a
  clean JSON error payload. Fixed.
- **`daemon start --dry-run` POSTed real webhooks.** The flag was only
  consulted on the `once` path, so the documented safety switch did nothing in
  loop mode. Fixed — it now reaches `DaemonService`, which owns the dispatcher.
- Hardcoded `active_windows` silently gated polling outside three windows;
  now empty means "no gating", active hours are opt-in.
- `tahuti --version` added (previously argparse-errored).
- **A bug this work introduced and then caught:** the rename commit updated the
  process-guard list in `daemon/__init__.py` but missed the duplicate in
  `daemon/system.py`, which still matched `"mb-cli"`. Since the daemon spawns
  `python -m mb_cli daemon run`, `daemon stop` could refuse to stop its own
  daemon. Both lists now match; `mb_cli` is retained deliberately.

### 4.4 Cleanup
Extracted the ~8-site `state.config_path.parent / "snapshot.json"` repetition
into `_snapshot_path()`, added missing return annotations, fixed a
`render_pretty` helper shadowing the `error()` payload helper.

### 4.5 Transport truth
**The MNN hub is a polled REST API, not a push or WebSocket channel.** This was
verified, not assumed: no WebSocket was ever attempted in any of the 183
commits (`git log --all -S'wss://' -S'ws://' -S'websocket'` returns nothing),
and both `mnn-hub.prod.faria.com` and `.cn` resolve via DNS — so the
constraint is *protocol*, not availability. The old `SPEC.md:39` had claimed
"WebSocket server URL" speculatively from the root commit and was never
validated; the code and its 2026-09-02 design spec always said REST. That file
was deleted on 2026-09-19 as stale — see
`docs/realtime-transport-findings.md` for the measured evidence.

Docs were corrected to match: README retitled "Event Engine", `library.md` and
`events.md` no longer claim real-time push, and `events.md` gained a
"Notification Transport: Polling, Not Push" section plus four guard tests
pinning it. **This matters for positioning** — the event engine is polling, and
should never be described as push.

### 4.6 Docs
ASCII box-drawing diagrams → mermaid in all user-facing docs. Email removed
from `SECURITY.md` entirely; GitHub Private Vulnerability Reporting is the sole
channel. Full rename sweep so no doc references a command or path that no
longer exists.

### 4.7 A platform bug the transfer itself found

Running the suite on Linux for the first time immediately failed one test:
`test_delete_reports_success` hardcoded macOS's `delete-generic-password` verb,
but on Linux `keychain.delete()` correctly issues `secret-tool clear`. The
*implementation* was right and the *assertion* was Darwin-only — the suite had
only ever run on macOS, so it had been reading green. Fixed in `537c259` by
asserting whichever verb the current platform issues. Worth remembering: this
repo's tests were single-platform until today, so treat "green on macOS" as
weak evidence for anything platform-dependent.

---

## 5. Competitive position (from a 25-project survey)

The ManageBac ecosystem is ~129 GitHub repos, ~50 student-facing, ~25 real.
**The official ManageBac API is admin-only** — students cannot get a key — so
every project here is a scraper, which is why none exceeds ~20 tools.

**Genuinely exclusive:** the typed event/webhook engine. Nobody else has typed
`MBEvent`s, HMAC-SHA256 signing, exponential backoff, active-hours scheduling,
stealth jitter, or launchd/systemd install. The nearest competitor is a
**daily 18:00 cron**. This is the only defensible claim the project has.

**Refuted differentiators:** the MCP server is *fourth* place — four ManageBac
MCP servers exist and two have GPA and grade weights we lack. Dual `.com`/`.cn`
support is weaker than advertised (rivals get `.cn` free by taking any base
URL); the real edge is the per-domain MNN hub mapping and the `ALLOWED_DOMAINS`
guard that stops the cookie and password being POSTed to an attacker-chosen host.

**Cheapest high-value gaps:** class-file download (we already parse attachments
at `client.py:529`), GPA rollup, subscribable `.ics`. Parent portal + multi-child
is the largest untapped audience but needs a second auth surface.

---

## 6. Known issues and open items

### Blocking a release
1. **Enable GitHub Private Vulnerability Reporting** (repo Settings → Code
   security and analysis). Until then `SECURITY.md`'s only reporting channel
   404s — and there is no email fallback by design.
2. **Three-platform verification** (§3).
3. **`daemon install` does not work on Windows.** `system.py` handles only
   Darwin (launchd) and Linux (systemd); anything else returns "Unsupported
   platform". Needs Task Scheduler (`schtasks`).
4. **`_is_tahuti_process` shells out to `ps`**, which does not exist on Windows.
   The failure is benign but wrong: `stop_background(verify_process=True)`
   would report "not a tahuti process" and refuse to stop. Needs a
   `wmic`/`tasklist` path.
5. **The Linux keychain path has never been verified working, on any host.**
   Added 2026-09-19 after establishing *why* it fails here (see §2.1). It is
   not a missing dependency, so it cannot be fixed by installing anything —
   which is what makes it a release blocker rather than a known limit. The
   macOS and Windows paths are exercised by tests and, for macOS, by real use;
   **Linux is exercised by neither.** Needs verification on a box with a
   graphical session — the Mac does not count, and this Pi cannot do it.

### Non-blocking, worth doing
- `docs/library.md` should get the same polling-not-push treatment as
  `events.md` if any "real-time" phrasing crept back in during merges.
- The service-label rename is **not migrated**: someone who ran
  `mb daemon install` before the rename still has `mb-daemon.service` loaded,
  and the new `uninstall` looks only for the new name → reports
  `service_file_missing`. Irrelevant with zero users; fix only if that changes.
- `count_grade_frequencies` / `count-grade-freq` has no consumer but itself.
  Either the seed of a GPA feature or dead weight.
- `docs/superpowers/` holds 9 internal design artifacts, several stale
  (`Draft`/`In Review`, some describing a pre-refactor tree). Kept in the repo
  deliberately, excluded from the sdist. Several still use ASCII diagrams —
  left as historical record.
- `daemon/webhook.py` (the HMAC/retry path — security-critical) has only 2
  tests. Thinnest coverage in the repo.
- `mb download` has tests now, but `daemon/system.py` and `daemon/state.py` are
  still thin (2 and 1 tests respectively).

### Credential-handling limits (documented in `SECURITY.md`)
Default remains cleartext `creds.json` at 0600 — the keychain is **opt-in**
(`--keychain` / `MB_CRAWLER_KEYCHAIN=1`), because flipping the default would
break headless CI. On Windows the Credential Locker **roams entries to the
Microsoft account by default**, with no flag to disable it, and `PasswordVault`
needs Windows PowerShell 5.1 (PowerShell 7 cannot load the WinRT type).

---

## 7. What was deliberately NOT done

- **No push, no publish, no release, no `gh` mutating calls.** Nothing has left
  the machine. The repo rename is **not** done — see §8.
- **No import-path rename** (`mb_cli` stays), no env-var rename, no flag renames.
- **`--interval` vs `--poll-interval`** asymmetry documented rather than
  unified — renaming a flag breaks users.
- **No `keyring` / `pywin32` / `pythonnet`.** The whole point of `keychain.py`
  is shelling out to helpers already on the OS.
- **`mcp` is bounded `>=1.20,<2`.** This is load-bearing: mcp 2.x removed
  `mcp.server.fastmcp`, which `mcp_server.py` imports, so an unbounded
  constraint ships a `tahuti-mcp` that dies on import. `uv.lock` had pinned
  1.27.2 and masked it locally.
- **No Windows `CredReadW`/`CredWriteW` via `ctypes`** — technically the
  strongest option (no child process, back to XP) but ~60 lines of struct
  layout that cannot be exercised without a Windows box. Recorded in the
  `keychain.py` docstring as the better alternative once hardware is available.
- **No migration code** for pre-rename state paths or service labels.

---

## 8. Repository state as transferred

The work was done in git worktrees on the Mac so parallel agents would not
collide. **Those worktrees were not copied here** — only this branch, with its
full history, is present. This is the single source of truth; nothing is lost,
because every branch was merged in first:

| Branch | Status |
|---|---|
| `publish-prep` | **checked out here** — 24 commits ahead of `2254a01` |
| `docs-fixes`, `cli-rename`, `windows-support` | merged into `publish-prep`, still present as refs |
| `worktree-finish-security-audit` | ancestor-less legacy clone; content already in `publish-prep`. Preserved for reference only |
| `main` | still at `2254a01` on the Mac; the work has **not** reached it |

The Mac's checkout at `~/Desktop/t8/mb-crawler` is unmodified and still on
`main`. It was not touched. `git remote` still points at
`github.com/allen546/mb-cli`, which has **not** been renamed.

**Not copied here on purpose:** `.venv` (64 MB, and it is macOS/Rosetta-built —
useless and possibly harmful on aarch64), `.claude/` (75 MB, agent scratch
including nested worktree clones), `.worktrees/`, and `dist/` (stale
`mb_cli-0.2.x`–`0.3.0` artifacts from *before* the rename — do not upload those).
Rebuild with `uv build`; it correctly produces `tahuti-0.3.0.{tar.gz,whl}`.

### Remaining repo-level steps (owner's call, all currently held)

1. **GitHub repo rename** — `allen546/mb-cli` → `allen546/tahuti`:
   ```bash
   gh repo rename tahuti --repo allen546/mb-cli
   ```
   GitHub redirects the old URL automatically, **but** `git remote` in any
   existing clone does not update itself — reset it after renaming. Do this
   before pushing, so history lands under the right name.
2. **Push `publish-prep`**, then merge to `main`.
3. **Enable private vulnerability reporting**, then publish to **TestPyPI
   first**, install from there to verify, before the real PyPI upload.
4. `gh` **is** installed here (2.90.0, `/home/linuxbrew/.linuxbrew/bin/gh`) — an
   earlier revision of this document said it was not. The token in
   `~/.config/gh/hosts.yml` is **invalid**, so `gh auth login` must be run by a
   human in an interactive terminal before any repo operation. Network is up
   (`github.com` and `api.github.com` both answer 200). A gated, phase-by-phase
   script covering rename → push → sdist inspection → TestPyPI → verification →
   PyPI is at `/mnt/pi-data/tahuti-imported/publish.sh` (outside the repo, since
   it holds personal data too). Do not upload the stale `mb_cli-0.2.x`–`0.3.0`
   artifacts in `dist/` — they predate the rename.

The Mac folder was *not* renamed to `tahuti/` — that step is moot here, since
this checkout already lives at `/mnt/pi-data/tahuti`.

---

## 8.5 Real-time notification transport — measured, not assumed

Written 2026-09-19 against the live account. Full detail and measurements in
`docs/realtime-transport-findings.md`; probes in `extras/probe_*.py`.

The question was whether tahuti can push notifications in true real time. Two
separate services are involved and they behave differently:

| Service | Transport | Evidence |
|---|---|---|
| `mnn-hub.prod.faria.cn` | **polled REST only** | 9 WebSocket paths → 404; 4 SSE paths → 404; 4 long-poll candidates all answered <0.1s; zero cable code in `MnnHub.es-*.js` and its 3 chunks |
| `<school>.managebac.cn/websocket` | **AnyCable WebSocket, real push** | `101 Switching Protocols`, `X-AnyCable-Version: 1.0.5-2b1cbd6`, sends `welcome` then a `ping` every 3s |

Two findings make fast polling the practical answer regardless:

1. **The hub answers in 75 ms** (median of 20 un-jittered requests; min 71.1,
   max 92.4).
2. **The hub honours `If-None-Match` on both endpoints** — `/notifications`
   returns 304 with **0 bytes** instead of 90774. A poll that costs 90 KB today
   costs nothing when idle.

Against that, `MNNHubClient._jitter()` (`notifications.py:37`) sleeps 1–3 s on
every `stats()` and `list()` call — **26× the cost of the transport itself**, and
inverted besides: it slows the two cheap reads and none of the five mutating
methods. There are no `X-RateLimit-*` or `Retry-After` headers, so nothing
declares a ceiling the jitter would be defending.

**Open question blocking a true-push design:** the AnyCable channel name.
Guessing produced no notification channel, and this is a *real* negative, not a
probe failure — established by control: `ProgressChannel` and `PresenceChannel`
both return `confirm_subscription`, `Chat::RoomChannel` returns
`reject_subscription`, and a deliberately bogus name returns silence. Since
AnyCable rejects channels it knows but will not authorise, silence means the
name does not exist. Six plausible notification names were all silent.

**One piece of browser evidence closes it:** with the notifications page open,
filter the Network tab to **WS** and read the `subscribe` frame's `identifier`.
Capture the identifier only — never the `Authorization` or JWT value.

If the bell has no WS frame at all, ManageBac has no push channel for
notifications and the ceiling is: poll `/notifications/stats` with
`If-None-Match` on a short timer, full-fetch only when the Etag moves.

---

## 9. Commit history on `publish-prep`

```
8c9f897 docs: document Windows keychain support and its limits
327b62c docs: finish the rename sweep the CLI change flagged
4737aad chore: regenerate uv.lock for the tahuti rename
dc4a576 feat(cli): make tahuti the primary command, keep mb/mb-mcp as aliases
1043a1b feat(keychain): add Windows Credential Locker support
b62b638 docs(security): drop the email fallback entirely
d8ef08f docs: make the docs truthful about transport and naming
7259ef9 fix(daemon): make --dry-run and active-hours behave as documented
13e07b5 security: close the credential-handling gap and make SECURITY.md truthful
bdf29ea fix(cli): repair interface defects found by audit and tidy the tree
bb6e701 refactor: stop render_pretty shadowing the error() payload helper
34d25be refactor: add missing return annotations to six helpers
bb9e80a refactor: extract snapshot-path and submission-state helpers in the CLI
5d1ec52 docs: correct the notification transport — MNN hub is REST, not WebSocket
aa09bc6 refactor: drop unused imports and dead local variables
d279cb6 refactor: drop the pre-rename config dir fallback
c2c65f0 feat: rename package to tahuti
```

Plus the earlier publish-readiness work already on the branch from
`2254a01`: sdist hygiene (a 75 MB nested worktree clone was shipping — the
sdist went from 153 entries to 71), `CHANGELOG.md`, `SECURITY.md`, GitHub
Actions CI, and the `[dependency-groups] dev` table declaring pytest,
requests-mock and mcp (none of which were declared, so a plain `uv sync`
could never have run the suite).
