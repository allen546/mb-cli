# Security Policy

## Supported Versions

Only the latest release line receives security fixes. Please upgrade before
reporting an issue against an older version.

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

0.3.x is the current line (`mb --version` reports `0.3.0`). Earlier lines are
unsupported and, because the project has not shipped a stable 1.0, may change
or drop interfaces without notice.

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

### Preferred channel: GitHub Private Vulnerability Reporting

Open a private report here:

> **<https://github.com/allen546/tahuti/security/advisories/new>**

This is the preferred channel: the report is visible only to the maintainers,
you get a private fork to collaborate on a fix, and we can publish a coordinated
advisory and CVE with credit to you.

> **OWNER ACTION REQUIRED — GitHub private vulnerability reporting must be
> enabled before this link works.** Until then the URL returns "not found",
> which reads as "no such repo" rather than "report here". To enable it:
> repository **Settings → Code security and analysis → Private vulnerability
> reporting → Enable**. No paid plan is required. Until that is switched on, use
> the email fallback below.

### Fallback: private email

If you do not have (or do not want to use) a GitHub account:

> **`SECURITY_CONTACT_PLACEHOLDER@example.invalid`**
>
> **OWNER ACTION REQUIRED — this is a placeholder and is NOT monitored.** Replace
> it with a real, privately monitored address before publishing, or delete this
> section if GitHub private reporting is the only channel you intend to run.

Include as much of the following as you can:

- A description of the vulnerability and its impact.
- The affected version (`mb --version`) and how you installed it (pip, uv, sdist).
- Steps to reproduce, or a minimal proof of concept.
- Any relevant logs, with credentials and session cookies redacted.

### What to expect

| Stage | Target |
| ----- | ------ |
| Acknowledgement of your report | **3 business days** |
| Initial assessment and severity triage | **7 business days** |
| Status update while a fix is in progress | every **14 days** |
| Fix released for a confirmed issue in a supported version | as soon as practical, targeted within **30 days** |

We will credit you in the release notes and the advisory unless you ask us not
to. We will not pursue legal action against good-faith research that respects
the scope below.

### Scope

In scope: the `mb_cli` Python package, the `mb` and `mb-mcp` console commands,
and `extras/mb-notifier/`.

Out of scope: anything on ManageBac's side; denial of service by polling
ManageBac aggressively; issues that require an attacker to already control your
user account, your machine, or your shell history. Note that this project's
disclaimer (see the README) already warns that automated access may violate
ManageBac's Terms of Service — that is a policy risk, not a vulnerability.

---

## Credential Storage Model and Its Limits

This tool needs your ManageBac password or session cookie to authenticate, so
understanding what is stored where matters.

By default `mb` keeps state in `~/.config/tahuti/` (override with
`MB_CRAWLER_CONFIG` / `MB_CRAWLER_SESSION` / `MB_CRAWLER_CREDS_PATH`):

| File | Contents | Protection |
| ---- | -------- | --------- |
| `config.json` | School domain, preferences, webhook URL | `0600`, in a `0700` dir |
| `session.json` | Authenticated session cookie | `0600`, in a `0700` dir |
| `creds.json` | **Your ManageBac password, in plaintext** | `0600`, in a `0700` dir |
| `snapshot.json` | Cached coursework (task titles, grades) | `0600`, in a `0700` dir |
| `cache/` | Cached HTTP responses, including grade pages and the MNN Hub JWT | `0600` files, `0700` dir |
| `daemon_state.json` | Notification/reminder dedup state | `0600`, in a `0700` dir |
| `daemon.log` / `daemon.pid` | Daemon runtime files | `0600`, in a `0700` dir |

### What this design does

- Every credential-bearing file is written `0600`, created via `mkstemp` and
  atomically `os.replace`d into place, so a plaintext password is never visible
  at a permissive mode even briefly. Directories are `0700`, including the
  parents that `mkdir(parents=True)` would otherwise leave at the umask default.
- **`mb logout` deletes the stored password.** It removes `creds.json` and any
  OS-keychain entry, in addition to clearing the session cookie and the response
  cache. Pass `--keep-credentials` if you want silent re-login preserved.
- **`mb login --temp` writes nothing to disk.** It sends `remember_me=0` to
  ManageBac, skips saving the password, skips saving the session cookie, and
  disables the response cache — the cache holds full grade pages and the hub JWT,
  so persisting it would have quietly defeated the flag.
- **Loose permissions are reported.** On startup `mb` warns on stderr if
  `creds.json`, `session.json`, or `config.json` is group- or world-readable.
  File permissions are the only barrier here, so a `0644` creds file is worth
  shouting about. Set `MB_CRAWLER_NO_PERM_WARN=1` to silence it.
- **An optional OS keychain exists.** `mb login --keychain` (or
  `MB_CRAWLER_KEYCHAIN=1`) stores the password in the macOS Keychain or the
  Linux Secret Service instead of `creds.json`. It adds no dependency — it
  shells out to `security` / `secret-tool` — and is strictly opt-in. `mb logout`
  deletes the keychain entry too.
- Secrets passed to a background daemon go through the child process's
  **environment** (`MB_WEBHOOK_SECRET`, `MB_CRAWLER_PASSWORD`,
  `MB_CRAWLER_COOKIE`) rather than `argv`, because `argv` is readable by any
  local user via `ps` for the life of the process.
- Webhook payloads are signed with HMAC-SHA256 and the bundled receiver in
  `extras/mb-notifier/` refuses unsigned, replayed (bounded id cache), or stale
  (±300s) pushes, comparing digests in constant time.

### What this design does **not** do — read this before using it

Be clear-eyed about the comparison set: rival ManageBac tools never store your
password at all (they keep a browser storage-state file), or encrypt it at rest
(AES256 with a local key). **We store your ManageBac password in cleartext.**
The keychain closes that gap only if you opt in. The honest position:

- **Default storage is cleartext at `0600`.** File permissions are the *only*
  barrier. Anything running as your user — malware, a compromised editor
  extension, a stray backup that drops modes — can read it. `root` and any
  process with `CAP_DAC_OVERRIDE` can read it regardless. This is exactly why
  the loose-permission warning exists: the barrier is thin and worth monitoring.
- **The keychain is optional and has real limits.** It is unlocked only while
  you are logged in, so on a headless Linux box with no secret service the store
  fails and `mb` falls back to `creds.json` with a warning rather than silently
  losing your credential. On macOS, `security add-generic-password` accepts a
  secret only as an argument, so the password is briefly visible in that
  short-lived child's `argv` — a window of milliseconds, after which the item is
  encrypted at rest by the login keychain. Linux `secret-tool` takes the secret
  on stdin and has no such exposure. A keychain item is also not covered by your
  normal file backups.
- **`--keep-credentials` deliberately re-opens the hole.** `mb logout` deletes
  the password by default because that is the safer default; the flag exists for
  users who prefer silent re-login over revocation. Know which one you are
  relying on.
- **`MB_CRAWLER_PASSWORD` and `MB_CRAWLER_COOKIE` are now read as input**, not
  just exported to the daemon child — so they work for non-interactive and CI
  use (`MB_CRAWLER_PASSWORD=... mb daemon run` needs no prompt), with an
  explicit `--password` / `--cookie` taking precedence. That also means a
  leaked environment variable is now directly usable as a credential, and a
  process's environment is still readable by its own user.
- **Webhook payloads contain student PII.** Concretely, the JSON body carries
  task titles, class names, class and task ids, due dates, `grade_letter` /
  `grade_score`, and a task URL — plus `sender` (the teacher or staff member who
  triggered it) and `body_preview` (a truncated verbatim excerpt of the ManageBac
  notification text). It does **not** contain the student's name or free-text
  teacher feedback or rubric comments; `mb feedback` fetches those separately and
  never ships them to a webhook. Point `--webhook-url` at `https://`; an
  `http://` endpoint sends that data unencrypted and accepts forged events from
  anything that can reach the port.
- **`--no-verify-tls` disables certificate verification** on the ManageBac
  client's requests session, which makes a man-in-the-middle trivial. It is
  offered for self-hosted instances behind a proxy with an untrusted CA. Do not
  use it on an untrusted network. Note that it covers ManageBac API traffic
  only — webhook delivery has its own `verify_tls` setting in the daemon config,
  which this flag does **not** change, so webhook POSTs still verify normally.
- **`--password` / `-p` on the command line lands in your shell history.** Prefer
  the interactive prompt or `MB_CRAWLER_PASSWORD`.

If any of these limits is unacceptable for your environment, run `mb` inside a
container or a dedicated locked-down user account.
