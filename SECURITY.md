# Security Policy

## Supported Versions

Only the latest release line receives security fixes. Please upgrade before
reporting an issue against an older version.

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :x:                |
| 0.1.x   | :x:                |

---

## Reporting a Vulnerability

<!--
  OWNER ACTION REQUIRED — PLACEHOLDER, NOT A REAL ADDRESS
  --------------------------------------------------------------------------
  The two contact values below are placeholders and are NOT monitored.
  Replace `SECURITY_CONTACT_PLACEHOLDER` with a real, privately monitored
  address (and add a GitHub Security Advisory URL if you enable GitHub
  private vulnerability reporting) before publishing to PyPI.
  --------------------------------------------------------------------------
-->

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Report them privately by email:

> **`SECURITY_CONTACT_PLACEHOLDER@example.invalid`**

Include as much of the following as you can:

- A description of the vulnerability and its impact.
- The affected version (`mb --version`) and how you installed it (pip, uv, sdist).
- Steps to reproduce, or a minimal proof of concept.
- Any relevant logs, with credentials and session cookies redacted.

If you would prefer not to use email, open a **private** GitHub Security
Advisory for this repository instead. If you are unsure which channel to use,
email is the safer default.

### What to expect

| Stage | Target |
| ----- | ------ |
| Acknowledgement of your report | **3 business days** |
| Initial assessment and severity triage | **7 business days** |
| Status update while a fix is in progress | every **14 days** |
| Fix released for a confirmed issue in a supported version | as soon as practical, targeted within **30 days** |

We will credit you in the release notes unless you ask us not to. We will not
pursue legal action against good-faith research that respects the scope below.

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

By default `mb` keeps state in `~/.config/mb-crawler/` (override with
`MB_CRAWLER_CONFIG` / `MB_CRAWLER_SESSION` / `MB_CRAWLER_CREDS_PATH`):

| File | Contents | Protection |
| ---- | -------- | --------- |
| `config.json` | School domain, preferences, webhook URL | `0700` directory |
| `session.json` | Authenticated session cookie | `0700` directory |
| `creds.json` | **Your ManageBac password, in plaintext** | `0600` file |
| `snapshot.json` | Cached coursework (task titles, grades) | `0700` directory |
| `cache/` | Cached HTTP responses, including grade pages and the MNN Hub JWT | `0700` directory |
| `daemon_state.json` | Notification/reminder dedup state | `0700` directory |
| `daemon.log` / `daemon.pid` | Daemon runtime files | `0700` directory |

### What this design does

- Every credential-bearing file is written `0600`, created via `mkstemp` and
  atomically `os.replace`d into place, so a plaintext password is never visible
  at a permissive mode even briefly. The directory is `0700`.
- `creds.json` is only written when a password login succeeds *without*
  `--temp`. Use `mb login --temp` for a session that is not persisted.
- Secrets passed to a background daemon go through the child process's
  **environment** (`MB_WEBHOOK_SECRET`, `MB_CRAWLER_PASSWORD`,
  `MB_CRAWLER_COOKIE`) rather than `argv`, because `argv` is readable by any
  local user via `ps` for the life of the process.
- Webhook payloads are signed with HMAC-SHA256 and the bundled receiver in
  `extras/mb-notifier/` refuses unsigned, replayed, or stale pushes.

### What this design does **not** do — read this before using it

- **There is no OS keychain integration.** Your password is plaintext on disk.
  File permissions are the *only* barrier. Anything running as your user —
  malware, a compromised editor extension, a stray backup that drops modes — can
  read it. `root` and any process with `CAP_DAC_OVERRIDE` can read it
  regardless.
- **`mb logout` does not delete `creds.json`.** It clears the session cookie and
  the response cache only. Remove the password yourself:
  ```bash
  rm ~/.config/mb-crawler/creds.json
  ```
- **`MB_CRAWLER_PASSWORD` and `MB_CRAWLER_COOKIE` are exported into the daemon
  child's environment but are not read back by the CLI.** They exist so a
  detached daemon can pick them up; do not rely on them as an input mechanism.
  A process's environment is still readable by its own user.
- **Webhook payloads contain student PII** — task titles, class names, due
  dates, grades, and teacher feedback. Point `--webhook-url` at `https://`; an
  `http://` endpoint sends that data unencrypted and accepts forged events from
  anything that can reach the port.
- **`--no-verify-tls` disables certificate verification** and is offered for
  self-hosted instances. It makes a man-in-the-middle trivial. Do not use it on
  an untrusted network.
- **`--password` / `-p` on the command line lands in your shell history.** Prefer
  the interactive prompt or `MB_CRAWLER_PASSWORD`.

If any of these limits is unacceptable for your environment, run `mb` inside a
container or a dedicated locked-down user account.
