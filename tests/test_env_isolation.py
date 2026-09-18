"""Guards for the global test-harness isolation in conftest.py.

Running this suite once deleted the operator's live ManageBac password:
`cmd_logout` resolves the credential file through `resolve_creds_path()`, and
nothing redirected `MB_CRAWLER_CREDS_PATH`, so it fell through to the real
~/.config/tahuti/creds.json and `clear_creds()` unlinked it. The autouse
`isolated_user_state` fixture is what stops that recurring; these tests are
what stop *it* from being removed or quietly narrowed.

Everything here is read-only with respect to the real ~/.config/tahuti. The
assertions are positive ("this resolver lands in the sandbox") rather than
"the real file is still there", because checking the real file's presence
would mean touching it.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from mb_cli import cache, daemon, keychain
from mb_cli import config
from mb_cli.config import (
    resolve_config_path,
    resolve_creds_path,
    resolve_session_path,
)
from tests.conftest import _redirected_paths

_MAIN = "mb_cli.__main__"
_DAEMON_STATE = "mb_cli.daemon.state"
_DAEMON_SYSTEM = "mb_cli.daemon.system"


def _const(module_name: str, attr: str) -> Path:
    """Read a path constant *through its module*.

    Never `from ... import` these into this module: they are bound at import
    time, so a top-level import here would capture the unpatched
    operator-home value before the autouse fixture redirects it — the test
    would then assert the very thing it exists to prevent, and pass.
    """
    return getattr(importlib.import_module(module_name), attr)


def _every_persisted_path() -> list[Path]:
    """Every filesystem path this package can read or write by default."""
    return [
        # Resolved at call time from the environment.
        resolve_config_path(),
        resolve_session_path(),
        resolve_creds_path(),
        # Bound at import time — unreachable by any environment variable.
        cache.DEFAULT_CACHE_DIR,
        _const(_MAIN, "DEFAULT_SNAPSHOT_PATH"),
        _const("mb_cli.daemon", "DEFAULT_SNAPSHOT_PATH"),
        _const("mb_cli.daemon", "DEFAULT_DAEMON_PATH"),
        _const(_DAEMON_STATE, "DEFAULT_STATE_PATH"),
        _const(_DAEMON_SYSTEM, "DEFAULT_PID_PATH"),
        _const(_DAEMON_SYSTEM, "DEFAULT_LOG_PATH"),
        # `daemon/__init__.py` re-exports these two under its own names.
        daemon.DEFAULT_PID_PATH,
        daemon.DEFAULT_LOG_PATH,
        config.config_dir(),
    ]


def _is_inside(path: Path, root: Path) -> bool:
    """True when *path* is *root* itself or lives beneath it."""
    return path == root or root in path.parents


class TestEveryDefaultPathIsSandboxed:
    """No default path may resolve outside the per-test sandbox."""

    def test_all_defaults_live_under_the_sandbox_config_dir(
        self, isolated_user_state: Path
    ):
        for path in _every_persisted_path():
            assert _is_inside(path, isolated_user_state), (
                f"{path} escaped the test sandbox; a test could read or delete "
                f"the operator's real ~/.config/tahuti through it"
            )

    def test_every_import_time_constant_is_patched_in_place(
        self, isolated_user_state: Path
    ):
        """Each constant is checked through its own module, because the same
        value is bound under more than one name.

        `daemon/__init__.py` does `from .system import DEFAULT_PID_PATH`, so
        patching only `daemon.system` would leave the package-level name — the
        one `load_daemon_config()` actually reads — pointed at the operator's
        home directory.
        """
        for target in _redirected_paths(Path("/nonexistent-sandbox")):
            module_name, attr = target.rsplit(".", 1)
            value = _const(module_name, attr)
            assert _is_inside(value, isolated_user_state), (
                f"{target} is {value}, outside the sandbox — it was bound at "
                f"import time and the environment cannot redirect it"
            )

    def test_no_default_points_at_the_real_user_config_dir(
        self, isolated_user_state: Path, real_user_config_dir: Path
    ):
        for path in _every_persisted_path():
            assert path != real_user_config_dir
            assert real_user_config_dir not in path.parents

    def test_home_is_redirected_so_uncached_home_lookups_are_safe(
        self, isolated_user_state: Path, tmp_path: Path, real_user_config_dir: Path
    ):
        """`Path.home()` is used directly by daemon/system.py for the launchd
        plist and the systemd user unit, which no constant covers."""
        assert Path.home() == tmp_path
        assert real_user_config_dir not in Path.home().parents


class TestNoRealCredentialStore:
    def test_keychain_helper_is_disabled(self):
        """`_load_creds()` falls back to the OS keychain when creds.json is
        missing; a helper installed on the dev box must not be consulted."""
        assert keychain.available() is False
        assert keychain.enabled() is False

    def test_credential_env_vars_are_absent_by_default(self):
        """A leaked MB_CRAWLER_* from the developer's shell must not survive
        into a test that never asked for it."""
        import os

        for var in (
            "MB_CRAWLER_PASSWORD",
            "MB_CRAWLER_COOKIE",
            "MB_CRAWLER_KEYCHAIN",
            "MB_CRAWLER_NO_PERM_WARN",
        ):
            assert var not in os.environ, (
                f"{var} leaked into the test environment; isolation clears it"
            )


class TestLogoutCannotReachTheRealCredentialFile:
    """The exact regression: `logout` deletes the creds file it resolves.

    On the pre-fix harness that resolved to the real ~/.config/tahuti/creds.json
    and `clear_creds()` unlinked the operator's ManageBac password.

    This test deliberately does *not* request `isolated_user_state` — it asks
    only for the two variables the original `test_logout` set, which is what
    makes it fail against the pre-fix harness instead of tautologically
    passing. Requesting the fixture would apply it and hide the regression.
    """

    def test_logout_deletes_only_a_sandboxed_creds_file(
        self, tmp_path: Path, monkeypatch, real_user_config_dir: Path
    ):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        # Guard first: if this ever resolves outside the sandbox, fail here
        # rather than writing a canary over a real credential file.
        resolved = resolve_creds_path()
        assert resolved != real_user_config_dir / "creds.json"
        assert real_user_config_dir not in resolved.parents, (
            f"{resolved} is the operator's real credential file; `logout` "
            f"would unlink it. MB_CRAWLER_CREDS_PATH must be redirected."
        )

        import json

        from mb_cli.__main__ import main

        canary = resolve_creds_path()
        canary.write_text(
            json.dumps({"email": "a@b.com", "version": 1}), encoding="utf-8"
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])
        assert exc_info.value.code == 0

        assert not canary.exists(), "logout did not delete the sandboxed creds file"

    def test_logout_resolves_creds_into_the_sandbox(self, isolated_user_state: Path):
        assert resolve_creds_path() == isolated_user_state / "creds.json"


class TestPerTestOverridesStillWin:
    """Isolation must not be something a test is trapped behind.

    A test that points a variable somewhere else has to get its way, otherwise
    the autouse fixture would silently break every per-test override.
    """

    def test_per_test_setenv_beats_the_sandbox(
        self, isolated_user_state: Path, monkeypatch
    ):
        chosen = isolated_user_state.parent / "elsewhere.json"
        monkeypatch.setenv("MB_CRAWLER_CREDS_PATH", str(chosen))
        assert resolve_creds_path() == chosen

    def test_per_test_setattr_beats_the_sandbox(self, monkeypatch):
        monkeypatch.setattr(cache, "DEFAULT_CACHE_DIR", Path("/tmp/elsewhere-cache"))
        assert cache.DEFAULT_CACHE_DIR == Path("/tmp/elsewhere-cache")
