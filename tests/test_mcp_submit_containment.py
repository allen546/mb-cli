"""tahuti's own state files must never be uploadable through a submit path.

Both entry points that upload a file — the ``submit_file`` MCP tool and the
``submit`` CLI command — go through one containment helper
(:func:`mb_cli.config.own_state_refusal`), so these tests run the same set of
paths through both and would catch either call site drifting from the other.

The exfiltration target is a school dropbox a teacher reads, and the MCP path
is driven by a model rather than by someone choosing a path on purpose — so the
threat is a confused tool call reaching for ``creds.json`` or a cached grade
page, not a deliberate one.  Scope is confined to tahuti's own directories:
files elsewhere on the system are deliberately left alone (see the module
comment in ``mb_cli/config.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.__main__ import build_parser, cmd_submit
from mb_cli.config import own_state_refusal
from mb_cli.mcp_server import submit_file

# Marker that stands in for a real secret.  Every state file below carries it so
# the tests can assert the refusal never echoes the file's contents.
SECRET = "TAHUTI-CONTAINMENT-MARKER-9f3c"
TASK_URL = "https://myschool.managebac.cn/student/classes/1000014/core_tasks/1000099"

#: One label per state file tahuti keeps for itself.
STATE_FILE_LABELS = (
    "creds",
    "session",
    "config",
    "daemon_state",
    "snapshot",
    "cache_entry",
)


class _LazyPathProxy:
    """Stands in for a path constant that only materialises on access."""

    def __init__(self, target: Path):
        self._target = target

    def __fspath__(self) -> str:
        return str(self._target)


def _state_paths() -> dict[str, Path]:
    """Every path holding tahuti's own state, keyed by a short label.

    Read from the live module attributes inside the test rather than bound at
    import, so ``conftest.isolated_user_state``'s redirect is what gets tested
    and a renamed constant fails loudly here instead of quietly going untested.
    """
    from mb_cli.__main__ import DEFAULT_SNAPSHOT_PATH
    from mb_cli.cache import DEFAULT_CACHE_DIR
    from mb_cli.config import (
        resolve_config_path,
        resolve_creds_path,
        resolve_session_path,
    )
    from mb_cli.daemon.state import DEFAULT_STATE_PATH

    return {
        "creds": resolve_creds_path(),
        "session": resolve_session_path(),
        "config": resolve_config_path(),
        "daemon_state": DEFAULT_STATE_PATH,
        "snapshot": DEFAULT_SNAPSHOT_PATH,
        # The cache is keyed by a hash of the login email; the layout under the
        # cache root is what matters here, not the key itself.
        "cache_entry": DEFAULT_CACHE_DIR / "0123456789abcdef" / "grades.json",
    }


@pytest.fixture()
def tahuti_state(isolated_user_state: Path) -> dict[str, Path]:
    """Write every tahuti-owned state file, each carrying *SECRET*."""
    paths = _state_paths()
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"secret": SECRET, "cookie": SECRET}), "utf-8")
    return paths


@pytest.fixture()
def mock_build_client():
    """Patch ``auth.build_client`` and hand back the mock and its client."""
    with patch("mb_cli.mcp_server.build_client") as mock:
        mock.return_value = (MagicMock(), MagicMock(), "student@example.com")
        yield mock, mock.return_value[1]


@pytest.fixture()
def patched_cli():
    """A fully mocked CLI client stack, for callers that need one to exist.

    Returned rather than entered so a test can assert afterwards that the
    command never got as far as using it.
    """
    client = MagicMock()
    client.submit_file.return_value = {"ok": True, "filename": "hw.pdf"}
    state = MagicMock()
    state.active_profile = "default"
    build = MagicMock(return_value=(state, client, "student@example.com"))
    with (
        patch("mb_cli.__main__._build_client", build),
        patch("mb_cli.__main__._authenticate_client"),
        patch("mb_cli.__main__._resolve_task_ids", return_value=("456", "1000026")),
        patch("mb_cli.__main__.load_snapshot", return_value={}),
        patch("mb_cli.__main__.find_task_by_id", return_value=None),
        patch("mb_cli.__main__.update_snapshot_with_class_tasks"),
    ):
        yield client


def _cli_args(file_path: Path) -> object:
    """``tahuti submit 1000026 <file_path>`` as parsed by the real parser."""
    return build_parser().parse_args(["submit", "1000026", str(file_path)])


# ── MCP tool ───────────────────────────────────────────────────────────


class TestMcpSubmitContainment:
    @pytest.mark.parametrize("label", STATE_FILE_LABELS)
    def test_refuses_each_of_tahutis_own_state_files(
        self, mock_build_client, tahuti_state, label
    ):
        mock, mock_client = mock_build_client
        state_file = tahuti_state[label]

        result = submit_file(task_id=TASK_URL, file_path=str(state_file))

        # An error payload, not a traceback and not a successful upload.
        payload = json.loads(result)
        assert "error" in payload, payload
        assert "Traceback" not in result
        assert state_file.name in payload["error"]
        assert "tahuti" in payload["error"]
        assert SECRET not in result
        mock_client.submit_file.assert_not_called()
        # No client is constructed for a path refused up front, so the refusal
        # costs no network round-trip either.
        mock.assert_not_called()

    def test_refuses_a_symlink_into_tahutis_state(
        self, mock_build_client, tahuti_state, tmp_path
    ):
        mock, mock_client = mock_build_client
        link = tmp_path / "homework-notes.json"
        link.symlink_to(tahuti_state["creds"])

        payload = json.loads(submit_file(task_id=TASK_URL, file_path=str(link)))

        assert "error" in payload
        # The refusal names the symlink's target: containment is decided on the
        # resolved path, so the link's innocent filename is not what was read.
        assert str(tahuti_state["creds"]) in payload["error"]
        assert SECRET not in json.dumps(payload)
        mock_client.submit_file.assert_not_called()
        mock.assert_not_called()

    def test_refuses_a_state_file_handed_over_as_a_lazy_path_proxy(
        self, mock_build_client, tahuti_state
    ):
        mock, mock_client = mock_build_client

        payload = json.loads(
            submit_file(task_id=TASK_URL, file_path=_LazyPathProxy(tahuti_state["creds"]))
        )

        assert "error" in payload
        mock_client.submit_file.assert_not_called()

    def test_refuses_a_creds_file_relocated_by_env_var(
        self, mock_build_client, tahuti_state, tmp_path, monkeypatch
    ):
        """``MB_CRAWLER_CREDS_PATH`` can put the password outside the config dir."""
        mock, mock_client = mock_build_client
        relocated = tmp_path / "backups" / "creds.json"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(json.dumps({"password": SECRET}), "utf-8")
        monkeypatch.setenv("MB_CRAWLER_CREDS_PATH", str(relocated))

        payload = json.loads(submit_file(task_id=TASK_URL, file_path=str(relocated)))

        assert "error" in payload
        assert SECRET not in json.dumps(payload)
        mock_client.submit_file.assert_not_called()

    def test_still_accepts_a_file_outside_tahutis_directories(
        self, mock_build_client, tahuti_state, tmp_path
    ):
        mock, mock_client = mock_build_client
        mock_client.submit_file.return_value = {"ok": True, "filename": "hw.pdf"}
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"%PDF-1.4 test")

        payload = json.loads(submit_file(task_id=TASK_URL, file_path=str(upload)))

        assert payload.get("ok") is True, payload
        assert mock_client.submit_file.call_args.args[2] == str(upload.resolve())

    def test_accepts_a_neighbour_of_a_relocated_state_file(
        self, mock_build_client, tahuti_state, tmp_path, monkeypatch
    ):
        """The refusal covers the state file itself, not the directory around it.

        This is the scope guard: only tahuti's own paths are refused, so a file
        sitting next to a relocated creds file is still an ordinary upload.
        """
        mock, mock_client = mock_build_client
        mock_client.submit_file.return_value = {"ok": True}
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("MB_CRAWLER_CREDS_PATH", str(elsewhere / "creds.json"))
        neighbour = elsewhere / "essay.pdf"
        neighbour.write_bytes(b"%PDF")

        payload = json.loads(submit_file(task_id=TASK_URL, file_path=str(neighbour)))

        assert payload.get("ok") is True, payload
        mock_client.submit_file.assert_called_once()


# ── CLI command ────────────────────────────────────────────────────────


class TestCliSubmitContainment:
    @pytest.mark.parametrize("label", STATE_FILE_LABELS)
    def test_refuses_each_of_tahutis_own_state_files(
        self, tahuti_state, label, capsys, patched_cli
    ):
        state_file = tahuti_state[label]

        rc = cmd_submit(_cli_args(state_file))

        assert rc == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["command"] == "submit"
        assert payload["error"]["code"] == "state_file_refused"
        assert state_file.name in payload["error"]["message"]
        assert "Traceback" not in out
        assert SECRET not in out
        # The whole point: the state file never reaches the uploader.
        patched_cli.submit_file.assert_not_called()

    def test_refuses_a_symlink_into_tahutis_state(
        self, tahuti_state, tmp_path, capsys, patched_cli
    ):
        link = tmp_path / "notes.txt"
        link.symlink_to(tahuti_state["session"])

        rc = cmd_submit(_cli_args(link))

        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["error"]["code"] == "state_file_refused"
        assert str(tahuti_state["session"]) in payload["error"]["message"]
        patched_cli.submit_file.assert_not_called()

    def test_refuses_before_any_client_is_built(self, tahuti_state, capsys):
        """A refused path costs no authentication round-trip."""
        with patch("mb_cli.__main__._build_client") as build:
            rc = cmd_submit(_cli_args(tahuti_state["creds"]))

        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["error"]["code"] == "state_file_refused"
        build.assert_not_called()

    def test_still_accepts_a_file_outside_tahutis_directories(
        self, tahuti_state, tmp_path, capsys, patched_cli
    ):
        upload = tmp_path / "hw.pdf"
        upload.write_bytes(b"%PDF-1.4 test")

        rc = cmd_submit(_cli_args(upload))

        assert rc == 0
        patched_cli.submit_file.assert_called_once_with("456", "1000026", str(upload))


# ── The helper itself ──────────────────────────────────────────────────


class TestOwnStateRefusal:
    def test_refuses_every_tahuti_state_path(self, tahuti_state):
        for path in tahuti_state.values():
            assert own_state_refusal(path) is not None, path

    def test_leaves_unrelated_paths_alone(self, tmp_path, tahuti_state):
        assert own_state_refusal(tmp_path / "essay.pdf") is None
        assert own_state_refusal(str(tmp_path)) is None
        # Explicitly out of scope: the operating system's own files are not
        # tahuti's business, and no rule here may grow into a sandbox.
        assert own_state_refusal("/etc/passwd") is None
        assert own_state_refusal(None) is None

    def test_does_not_guess_at_a_path_that_will_not_resolve(self):
        """An unresolvable path is the caller's error to report, not this one's."""
        assert own_state_refusal("\x00not a path") is None

    def test_refusal_names_the_field_the_caller_validates(self, tahuti_state):
        refusal = own_state_refusal(tahuti_state["creds"], field="file_path")
        assert refusal is not None
        assert refusal.startswith("file_path ")
