"""Shared client construction and authentication for CLI and MCP."""

from __future__ import annotations

from .cache import ResponseCache
from .client import ManageBacClient
from .config import AppState, load_state
from .exceptions import CommandError


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
    elif state.session.cookie and not reauth:
        client.set_cookie(state.session.cookie)
    else:
        if not email_val:
            raise CommandError("missing_credentials", "Missing email in args or config")
        if not password:
            raise CommandError(
                "missing_credentials",
                "Password required — pass password= or configure a session",
            )
        if not client.login(email_val, password):
            raise CommandError("authentication_failed", "ManageBac login failed")

    return state, client, email_val or ""
