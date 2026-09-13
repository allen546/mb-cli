import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.cache import ResponseCache
from mb_cli.client import ManageBacClient
from mb_cli.__main__ import (
    _resolve_task_ids,
    update_snapshot_with_class_tasks,
    load_snapshot,
    save_snapshot,
    cmd_submit,
    cmd_list,
    build_parser,
)


def test_invalidate_task_cache(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True, ttl=1800)
    client = ManageBacClient("bj80", domain="managebac.cn", cache=cache)

    base = client.base
    task_url = f"{base}/student/classes/101/core_tasks/202"
    dropbox_url = f"{base}/student/classes/101/core_tasks/202/dropbox"
    hint_url = f"{base}/student/classes/101/events/202/hint"
    core_tasks_url = f"{base}/student/classes/101/core_tasks"
    unrelated_url = f"{base}/student/classes/999/core_tasks/888"

    cache.put(task_url, "task detail", 200)
    cache.put(dropbox_url, "dropbox page", 200)
    cache.put(hint_url, "hint page", 200)
    cache.put(core_tasks_url, "class core tasks", 200)
    cache.put(unrelated_url, "unrelated page", 200)

    # Invalidate task 202 in class 101
    client.invalidate_task_cache("101", "202")

    assert cache.get(task_url) is None
    assert cache.get(dropbox_url) is None
    assert cache.get(hint_url) is None
    assert cache.get(core_tasks_url) is None
    assert cache.get(unrelated_url) is not None


def test_get_class_tasks(tmp_path: Path):
    cache = ResponseCache(cache_dir=tmp_path / "cache", enabled=True)
    client = ManageBacClient("bj80", domain="managebac.cn", cache=cache)

    mock_class_data = {
        "tasks": [
            {
                "task_id": "111",
                "title": "HW 1",
                "url": "https://bj80.managebac.cn/student/classes/101/core_tasks/111",
                "points": "10",
                "grade_letter": "A",
                "due_date": "Sep 20, 5:00 PM",
                "status": "submitted",
                "has_submit_button": False,
                "labels": ["Homework", "Submitted"],
            },
            {
                "task_id": "222",
                "title": "HW 2",
                "url": "https://bj80.managebac.cn/student/classes/101/core_tasks/222",
                "points": None,
                "grade_letter": None,
                "due_date": "Oct 1, 5:00 PM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Homework", "Pending"],
            },
        ]
    }

    with patch.object(client, "get_class_grades", return_value=mock_class_data):
        tasks = client.get_class_tasks("101", class_name="Physics", bypass_cache=True)

    assert len(tasks) == 2
    assert tasks[0]["id"] == "111"
    assert tasks[0]["class_name"] == "Physics"
    assert tasks[0]["status"] == "submitted"
    assert tasks[1]["id"] == "222"
    assert tasks[1]["status"] == "not-submitted"
    assert tasks[1]["has_submit_button"] is True


def test_resolve_task_ids_snapshot_fast_path(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_data = {
        "crawled_at": "2026-09-13T10:00:00",
        "upcoming": [
            {
                "id": "27546408",
                "title": "kinematics",
                "link": "https://beijing101.managebac.cn/student/classes/11516105/core_tasks/27546408",
            }
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, snapshot_data)

    client = MagicMock()
    # crawl_all should NOT be called because the task is in the snapshot!
    client.crawl_all.side_effect = RuntimeError("Should not crawl!")

    cid, tid = _resolve_task_ids(client, "27546408", snapshot_path=snapshot_path)
    assert cid == "11516105"
    assert tid == "27546408"
    client.crawl_all.assert_not_called()


def test_resolve_task_ids_fallback_to_crawl(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    save_snapshot(snapshot_path, {"upcoming": [], "past": [], "overdue": []})

    client = MagicMock()
    client.crawl_all.return_value = {
        "upcoming": [
            {
                "id": "99999",
                "link": "https://beijing101.managebac.cn/student/classes/12345/core_tasks/99999",
            }
        ],
        "past": [],
        "overdue": [],
    }

    cid, tid = _resolve_task_ids(client, "99999", snapshot_path=snapshot_path)
    assert cid == "12345"
    assert tid == "99999"
    client.crawl_all.assert_called_once()


def test_update_snapshot_with_class_tasks(tmp_path: Path):
    snapshot_path = tmp_path / "snapshot.json"
    initial_snapshot = {
        "crawled_at": "2026-09-13T12:00:00",
        "upcoming": [
            {
                "id": "1",
                "title": "Task 1",
                "class_name": "Math",
                "due_date": "Dec 1, 10:00 AM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Pending"],
            },
            {
                "id": "2",
                "title": "Task 2",
                "class_name": "Physics",
                "due_date": "Dec 2, 10:00 AM",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Pending"],
            },
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, initial_snapshot)

    # Updated Physics tasks (Task 2 submitted, Task 3 added)
    updated_physics_tasks = [
        {
            "id": "2",
            "title": "Task 2",
            "class_name": "Physics",
            "due_date": "Dec 2, 10:00 AM",
            "status": "submitted",
            "has_submit_button": False,
            "labels": ["Submitted"],
        },
        {
            "id": "3",
            "title": "Task 3",
            "class_name": "Physics",
            "due_date": "Dec 5, 10:00 AM",
            "status": "not-submitted",
            "has_submit_button": True,
            "labels": ["Pending"],
        },
    ]

    client = MagicMock()
    updated = update_snapshot_with_class_tasks(
        snapshot_path, updated_physics_tasks, client=client
    )

    loaded = load_snapshot(snapshot_path)
    # Task 1 (Math) should still be present and unmodified
    t1 = next(t for t in loaded["upcoming"] if t["id"] == "1")
    assert t1["status"] == "not-submitted"

    # Task 2 should now be submitted!
    t2 = next(t for t in loaded["upcoming"] if t["id"] == "2")
    assert t2["status"] == "submitted"
    assert t2["has_submit_button"] is False

    # Task 3 should be added
    t3 = next(t for t in loaded["upcoming"] if t["id"] == "3")
    assert t3["title"] == "Task 3"


def test_cmd_submit_eager_refresh_end_to_end(tmp_path: Path, capsys):
    parser = build_parser()
    submit_args = parser.parse_args(["submit", "27546408", str(tmp_path / "work.pdf")])
    (tmp_path / "work.pdf").write_bytes(b"%PDF-test")

    # Prepare snapshot where task 27546408 is unsubmitted
    snapshot_path = tmp_path / "snapshot.json"
    initial_snapshot = {
        "crawled_at": "2026-09-13T12:00:00",
        "student_name": "Test Student",
        "school": "beijing101",
        "base_url": "https://beijing101.managebac.cn",
        "upcoming": [
            {
                "id": "27546408",
                "title": "kinematics classwork1",
                "class_name": "AP Physics 1",
                "due_date": "Dec 13, 5:55 PM",
                "link": "https://beijing101.managebac.cn/student/classes/11516105/core_tasks/27546408",
                "status": "not-submitted",
                "has_submit_button": True,
                "labels": ["Formative", "Pending"],
            }
        ],
        "past": [],
        "overdue": [],
    }
    save_snapshot(snapshot_path, initial_snapshot)

    mock_state = MagicMock()
    mock_state.config_path = tmp_path / "config.json"
    mock_state.active_profile = "default"

    mock_client = MagicMock()
    mock_client.base = "https://beijing101.managebac.cn"
    mock_client.submit_file.return_value = {
        "ok": True,
        "filename": "work.pdf",
        "task_url": "https://beijing101.managebac.cn/student/classes/11516105/core_tasks/27546408",
    }
    # Return fresh class tasks where task 27546408 is submitted
    mock_client.get_class_tasks.return_value = [
        {
            "id": "27546408",
            "title": "kinematics classwork1",
            "class_name": "AP Physics 1",
            "due_date": "Dec 13, 5:55 PM",
            "link": "https://beijing101.managebac.cn/student/classes/11516105/core_tasks/27546408",
            "status": "submitted",
            "has_submit_button": False,
            "labels": ["Formative", "Submitted"],
        }
    ]

    with (
        patch("mb_cli.__main__._build_client", return_value=(mock_state, mock_client, "user@test.com")),
        patch("mb_cli.__main__._authenticate_client"),
    ):
        code = cmd_submit(submit_args)
        assert code == 0

    # Verify snapshot was eagerly refreshed to submitted!
    snap = load_snapshot(snapshot_path)
    updated_t = snap["upcoming"][0]
    assert updated_t["status"] == "submitted"
    assert updated_t["has_submit_button"] is False
    assert "Submitted" in updated_t["labels"]

    # Now verify that cmd_list with --todo excludes this submitted task!
    list_args = parser.parse_args(["list", "--todo"])
    with (
        patch("mb_cli.__main__._build_client", return_value=(mock_state, mock_client, "user@test.com")),
        patch("mb_cli.__main__._authenticate_client"),
    ):
        code = cmd_list(list_args)
        assert code == 0

    captured = capsys.readouterr()
    # The submitted task should not appear in the todo list
    assert "kinematics classwork1" not in captured.out
