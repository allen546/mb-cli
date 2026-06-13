"""Shared client construction and authentication for CLI and MCP."""

from __future__ import annotations

import logging

from .cache import ResponseCache
from .client import ManageBacClient
from .config import AppState, load_creds, load_state
from .exceptions import CommandError

log = logging.getLogger(__name__)

_CREDS_PATH = "/mnt/pi-data/tools/mb_config.json"


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

    resolved_ttl = (
        cache_ttl if cache_ttl is not None else state.profile.default_cache_ttl
    )
    cache = ResponseCache(enabled=not refresh, ttl=resolved_ttl)
    client = ManageBacClient(
        school, domain=domain, cache=cache, verify=verify, retry=retry
    )

    email_val = email or state.profile.email or state.session.email

    if cookie:
        client.set_cookie(cookie)
    elif password:
        if not email_val:
            raise CommandError(
                "missing_credentials", "Missing email for password login"
            )
        if not client.login(email_val, password, remember=remember):
            raise CommandError("authentication_failed", "ManageBac login failed")
    elif state.session.cookie and not reauth:
        # Health check: try saved cookie, re-login if stale
        client.set_cookie(state.session.cookie)
        if _is_session_alive(client):
            pass  # cookie is good
        else:
            log.info("Saved cookie expired — attempting silent re-login")
            _relogin_from_creds(client, state)
    else:
        if not email_val:
            raise CommandError("missing_credentials", "Missing email in args or config")
        if not password:
            raise CommandError(
                "missing_credentials",
                "Password required — pass password= or configure a session",
            )
        if not client.login(email_val, password, remember=remember):
            raise CommandError("authentication_failed", "ManageBac login failed")

    return state, client, email_val or ""


def _is_session_alive(client: ManageBacClient) -> bool:
    """Lightweight health check — GET the base URL, return True if not redirected to login.

    NOTE: This check is coupled to the ``/login`` path — if ManageBac changes
    its login URL the heuristic breaks silently.  A redirect to any other path
    would also be treated as "alive", which is acceptable for this use-case.
    """
    try:
        # The GET discards cookies/state from the server's perspective; we only
        # observe whether the response URL indicates a login redirect.
        r = client.session.get(f"{client.base}/")
        return "/login" not in r.url
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
