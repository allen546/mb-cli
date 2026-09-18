"""Shared client construction and authentication for CLI and MCP."""

from __future__ import annotations

import logging

from .cache import ResponseCache
from .client import ManageBacClient
from . import keychain
from .config import (
    AppState,
    clear_creds,
    load_creds,
    load_state,
    resolve_creds_path,
    save_creds,
    save_session,
)
from .exceptions import CommandError
from .notifications import MNNHubClient

log = logging.getLogger(__name__)


def hub_client(
    endpoint: str, token: str, *, verify: bool | str, timeout: float = 15.0
) -> MNNHubClient:
    """Build the MNN-hub client, honouring the caller's TLS decision.

    The single construction point for :class:`~mb_cli.notifications.MNNHubClient`.
    ``verify`` is keyword-only and has no default on purpose: six call sites
    used to build the client directly and so kept ``verify=True``, which made
    ``--no-verify-tls`` apply to ManageBac and not to the hub. A user who
    explicitly accepted a self-signed or internal CA for the school host got a
    certificate error from the hub instead, with nothing in the message saying
    why. A call site that forgets the argument now fails with ``TypeError``
    rather than silently reverting to the stricter policy.

    Pass ``client.session.verify`` from an already-built
    :class:`~mb_cli.client.ManageBacClient` so the hub cannot diverge from
    whatever TLS decision that client is using.
    """
    return MNNHubClient(endpoint, token, verify=verify, timeout=timeout)


def _creds_path() -> str:
    """Resolve the creds path per-call so tests can redirect it via env.

    ``build_client`` and friends must never touch the developer's real saved
    password when running under pytest.

    This is the *only* way any code here resolves that path — storing, reading,
    deleting and reporting all funnel through it. It used to be captured as a
    module-level constant at import while this function re-resolved dynamically,
    so a caller that imported ``_CREDS_PATH`` and a caller that called
    ``_creds_path()`` could name two different files: one would write the
    password to ``~/.config/tahuti/creds.json`` while the other went looking for
    it in ``~/.config/mb-crawler/creds.json`` and reported ``missing_credentials``.
    """
    return str(resolve_creds_path())


def _store_password(email: str, password: str, use_keychain: bool | None = None) -> str:
    """Persist a password for silent re-login.

    Returns the backend actually used: ``"keychain"``, ``"file"``, or
    ``"none"``. The OS keychain is opt-in and preferred when available; without
    it the password lands in the cleartext 0600 ``creds.json`` that
    :mod:`mb_cli.config` writes. A keychain that fails to store falls back to
    the file rather than silently losing the credential.
    """
    if keychain.enabled(use_keychain):
        if keychain.store(email, password):
            # Drop any cleartext copy left by an earlier non-keychain login, so
            # switching backends does not leave the password on disk twice.
            clear_creds(_creds_path())
            return "keychain"
        log.warning("OS keychain unavailable — falling back to creds.json")
    save_creds(_creds_path(), email, password)
    return "file"


def _load_creds(email_hint: str | None = None) -> dict | None:
    """Load saved credentials, consulting the OS keychain as a fallback.

    ``creds.json`` wins when it holds a password so an existing install keeps
    working unchanged. The keychain is consulted when the file is missing or
    carries no password — i.e. after ``tahuti login --keychain`` — using the
    profile/session email as the account name.
    """
    creds = load_creds(_creds_path())
    if creds and creds.get("password"):
        return creds
    account = (creds or {}).get("email") or email_hint
    if account and keychain.available():
        secret = keychain.lookup(account)
        if secret:
            merged = dict(creds or {})
            merged["email"] = account
            merged["password"] = secret
            return merged
    return creds


def session_email(state: AppState, override: str | None = None) -> str:
    """Return the email that identifies this profile's on-disk state.

    One function because two things are keyed by it and they used to disagree
    about which email they meant: the response-cache directory is a hash of it,
    and the OS-keychain item is filed under it. ``build_client`` preferred an
    explicit ``--email``, then the profile's email, then the session's; the
    ``logout`` handler took the session's first. With the two set to different
    values, ``logout`` deleted a *different* profile's hash directory and left
    the JWT-bearing entries in place while still reporting success — and left
    the keychain item behind for the account it actually deleted nothing for.

    ``logout`` passes no override (its subparser defines no ``--email``), so the
    two agree on ``profile.email or session.email``. Returns ``""`` rather than
    ``None`` when neither is set, so callers can treat the result as a string.
    """
    return override or state.profile.email or state.session.email or ""


def build_client(
    school: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    password: str | None = None,
    cookie: str | None = None,
    profile: str | None = None,
    refresh: bool = False,
    reauth: bool = False,
    verify: bool | str = True,
    cache_ttl: int | None = None,
    retry: int = 3,
    remember: bool = True,
    use_keychain: bool | None = None,
) -> tuple[AppState, ManageBacClient, str]:
    """Build and authenticate a :class:`ManageBacClient`.

    Returns ``(state, client, email)``.  Raises :class:`CommandError` on
    missing credentials or authentication failure.

    *use_keychain* overrides ``MB_CRAWLER_KEYCHAIN`` for this call only;
    ``None`` defers to the environment.
    """
    state = load_state(profile)
    school = school or state.profile.school or state.session.school
    domain = domain or state.profile.domain or state.session.domain or "managebac.com"

    if not school:
        raise CommandError("missing_credentials", "Missing school in args or config")

    email_val = session_email(state, email)
    if not email_val:
        try:
            creds = _load_creds()
            if creds:
                email_val = creds.get("email")
        except Exception:
            pass

    import hashlib
    from .cache import DEFAULT_CACHE_DIR
    if email_val:
        email_hash = hashlib.sha256(email_val.encode()).hexdigest()[:16]
        cache_dir = DEFAULT_CACHE_DIR / email_hash
    else:
        cache_dir = DEFAULT_CACHE_DIR

    resolved_ttl = (
        cache_ttl if cache_ttl is not None else state.profile.default_cache_ttl
    )
    # `remember=False` (`tahuti login --temp`) must leave nothing on disk, and the
    # response cache holds full grade pages plus the MNN-hub JWT — so the cache
    # is disabled too, not just the saved password.
    cache = ResponseCache(
        cache_dir=cache_dir, enabled=not refresh and remember, ttl=resolved_ttl
    )
    client = ManageBacClient(
        school, domain=domain, cache=cache, verify=verify, retry=retry
    )

    if cookie:
        client.set_cookie(cookie)
    elif password:
        if not email_val:
            raise CommandError(
                "missing_credentials", "Missing email for password login"
            )
        if not client.login(email_val, password, remember=remember):
            raise CommandError("authentication_failed", "ManageBac login failed")
        # `remember=False` (mb --temp) means "do not persist my password to
        # disk".  Persisting it anyway would silently defeat that flag.
        if remember:
            _store_password(email_val, password, use_keychain)
    elif state.session.cookie and not reauth:
        # Health check: try saved cookie, re-login if stale
        client.set_cookie(state.session.cookie)
        if _is_session_alive(client):
            pass  # cookie is good
        else:
            log.info("Saved cookie expired — attempting silent re-login")
            _relogin_from_creds(client, state)
    else:
        # No session cookie and no explicit password — try loading from config
        creds = _load_creds(email_val)
        login_email = email_val or (creds.get("email") if creds else None)
        login_pass = password or (creds.get("password") if creds else None)
        if not login_email or not login_pass:
            raise CommandError(
                "missing_credentials",
                "No session, no password — pass password= or set a password via `tahuti login`",
            )
        if not client.login(login_email, login_pass, remember=remember):
            raise CommandError("authentication_failed", "ManageBac login failed")
        # Persist new session — unless this is a `--temp` login, which must not
        # leave a reusable cookie behind any more than it leaves a password.
        if remember:
            state.session.cookie = client.session.cookies.get("_managebac_session")
            state.session.logged_in_at = __import__("datetime").datetime.now().isoformat()
            state.session.school = school
            state.session.domain = domain
            state.session.email = login_email
            save_session(state)

    return state, client, email_val or ""


def _is_session_alive(client: ManageBacClient) -> bool:
    """Lightweight health check — GET a protected page, return True if session is valid.

    Checks both for login redirects (3xx → /login) and auth failures (401/403).
    Uses a page that requires authentication so an expired session reliably redirects.
    """
    try:
        # Use allow_redirects=False so we can inspect the Location header directly.
        # r.url always reflects the *request* URL, never the redirect target.
        r = client.session.get(
            f"{client.base}/student/dashboard", allow_redirects=False
        )
        if r.status_code in (401, 403):
            return False
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            return "/login" not in location
        # 200 OK on an auth-required page means the session is valid
        return True
    except Exception:
        return False


def _relogin_from_creds(client: ManageBacClient, state: AppState) -> None:
    """Re-login using saved credentials. Raises CommandError on failure."""
    creds = _load_creds(state.session.email or state.profile.email)
    if not creds or "email" not in creds or "password" not in creds:
        raise CommandError(
            "missing_credentials",
            f"Cookie expired and no creds found in {_creds_path()}",
        )
    if not client.login(creds["email"], creds["password"], remember=True):
        raise CommandError("authentication_failed", "Silent re-login failed")
    # Persist the new cookie so subsequent calls don't re-login
    state.session.cookie = client.session.cookies.get("_managebac_session")
    state.session.logged_in_at = __import__("datetime").datetime.now().isoformat()
    save_session(state)
