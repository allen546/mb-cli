"""Shared client construction and authentication for CLI and MCP."""

from __future__ import annotations

import logging
from pathlib import Path

from .cache import ResponseCache
from .client import ManageBacClient
from .config import AppState, load_creds, load_state, save_creds, save_session
from .exceptions import CommandError

log = logging.getLogger(__name__)

_CREDS_PATH = str(Path.home() / ".config" / "mb-crawler" / "creds.json")


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
) -> tuple[AppState, ManageBacClient, str]:
    """Build and authenticate a :class:`ManageBacClient`.

    Returns ``(state, client, email)``.  Raises :class:`CommandError` on
    missing credentials or authentication failure.
    """
    state = load_state(profile)
    school = school or state.profile.school or state.session.school
    domain = domain or state.profile.domain or state.session.domain or "managebac.com"

    if not school:
        raise CommandError("missing_credentials", "Missing school in args or config")

    email_val = email or state.profile.email or state.session.email
    if not email_val:
        try:
            creds = load_creds(_CREDS_PATH)
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
    cache = ResponseCache(cache_dir=cache_dir, enabled=not refresh, ttl=resolved_ttl)
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
        save_creds(_CREDS_PATH, email_val, password)
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
        creds = load_creds(_CREDS_PATH)
        login_email = email_val or (creds.get("email") if creds else None)
        login_pass = password or (creds.get("password") if creds else None)
        if not login_email or not login_pass:
            raise CommandError(
                "missing_credentials",
                "No session, no password — pass password= or configure mb_config.json",
            )
        if not client.login(login_email, login_pass, remember=remember):
            raise CommandError("authentication_failed", "ManageBac login failed")
        # Persist new session
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
    """Re-login using credentials from mb_config.json. Raises CommandError on failure."""
    creds = load_creds(_CREDS_PATH)
    if not creds or "email" not in creds or "password" not in creds:
        raise CommandError(
            "missing_credentials",
            f"Cookie expired and no creds found in {_CREDS_PATH}",
        )
    if not client.login(creds["email"], creds["password"], remember=True):
        raise CommandError("authentication_failed", "Silent re-login failed")
    # Persist the new cookie so subsequent calls don't re-login
    state.session.cookie = client.session.cookies.get("_managebac_session")
    state.session.logged_in_at = __import__("datetime").datetime.now().isoformat()
    save_session(state)
