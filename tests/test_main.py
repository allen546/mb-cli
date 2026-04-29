"""Tests for mb_cli.__main__ (CLI entry-point)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mb_cli.__main__ import build_parser, main


class TestBuildParser:
    def test_has_all_subcommands(self):
        parser = build_parser()
        subactions = [
            a for a in parser._subparsers._actions if hasattr(a, "_parser_class")
        ]
        assert len(subactions) == 1
        subparser = subactions[0]
        choices = subparser.choices
        expected = {
            "login",
            "list",
            "view",
            "logout",
            "daemon",
            "submit",
            "notifications",
            "calendar",
            "timetable",
            "grades",
            "count-grade-freq",
        }
        assert set(choices.keys()) == expected

    def test_login_defaults(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "login",
                "--school",
                "bj80",
                "-e",
                "a@b.com",
                "-p",
                "pass",
                "-d",
                "managebac.cn",
            ]
        )
        assert args.school == "bj80"
        assert args.email == "a@b.com"
        assert args.password == "pass"
        assert args.domain == "managebac.cn"

    def test_list_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        assert args.view is None
        assert args.details is None
        assert args.pages is None

    def test_view_with_target(self):
        parser = build_parser()
        args = parser.parse_args(["view", "12345"])
        assert args.target == "12345"

    def test_submit_args(self):
        parser = build_parser()
        args = parser.parse_args(["submit", "12345", "file.pdf"])
        assert args.target == "12345"
        assert args.file == "file.pdf"

    def test_notifications_args(self):
        parser = build_parser()
        args = parser.parse_args(["notifications", "--page", "2", "--per-page", "10"])
        assert args.page == 2
        assert args.per_page == 10

    def test_daemon_subcommands(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "daemon",
                "start",
                "--once",
                "--dry-run",
                "--school",
                "bj80",
                "-e",
                "a@b.com",
                "-p",
                "x",
                "-c",
                "cookie",
            ]
        )
        assert args.daemon_command == "start"
        assert args.once is True
        assert args.dry_run is True

    def test_daemon_stop(self):
        parser = build_parser()
        args = parser.parse_args(["daemon", "stop"])
        assert args.daemon_command == "stop"

    def test_daemon_configure_webhook(self):
        parser = build_parser()
        args = parser.parse_args(
            ["daemon", "configure-webhook", "http://localhost:8080/hook"]
        )
        assert args.url == "http://localhost:8080/hook"

    def test_calendar_args(self):
        parser = build_parser()
        args = parser.parse_args(
            ["calendar", "--start", "2026-04-01", "--end", "2026-04-30", "--today"]
        )
        assert args.start == "2026-04-01"
        assert args.end == "2026-04-30"
        assert args.today is True

    def test_timetable_args(self):
        parser = build_parser()
        args = parser.parse_args(["timetable", "--date", "2026-04-28", "--today"])
        assert args.date == "2026-04-28"
        assert args.today is True

    def test_grades_args(self):
        parser = build_parser()
        args = parser.parse_args(
            ["grades", "--class-id", "11460711", "--subject", "Math"]
        )
        assert args.class_id == "11460711"
        assert args.subject == "Math"

    def test_count_grade_freq_args(self):
        parser = build_parser()
        args = parser.parse_args(["count-grade-freq", "--subject", "EL"])
        assert args.subject == "EL"

    def test_logout_args(self):
        parser = build_parser()
        args = parser.parse_args(["logout", "--all"])
        assert args.all is True


def _mock_build_client_result(mock_client, email="a@b.com"):
    """Create the (state, client, email) tuple that _build_client returns."""
    mock_state = MagicMock()
    mock_state.active_profile = "default"
    mock_state.profile.default_subject = ""
    mock_state.profile.default_view = "all"
    mock_state.profile.default_details = False
    mock_state.profile.default_pages = 10
    return mock_state, mock_client, email


class TestMainLogin:
    def test_login_success(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.school = "bj80"
        mock_client.domain = "managebac.cn"
        mock_client.base = "https://bj80.managebac.cn"
        mock_client.session.cookies.get.return_value = "session_cookie"

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(
                mock_client, "test@example.com"
            )
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print") as mock_print:
                        with pytest.raises(SystemExit) as exc_info:
                            main(
                                [
                                    "login",
                                    "--school",
                                    "bj80",
                                    "-e",
                                    "test@example.com",
                                    "-p",
                                    "pass",
                                    "-d",
                                    "managebac.cn",
                                    "--format",
                                    "json",
                                ]
                            )
                        assert exc_info.value.code == 0


class TestMainList:
    def test_list_success(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.domain = "managebac.cn"
        mock_client.crawl_all.return_value = {
            "student_name": "John",
            "school": "bj80",
            "base_url": "https://bj80.managebac.cn",
            "crawled_at": "2026-04-29T12:00:00",
            "upcoming": [
                {"id": "1", "title": "HW1", "class_name": "Math", "due_date": "May 1"}
            ],
            "past": [],
            "overdue": [],
            "summary": {"upcoming_count": 1, "past_count": 0, "overdue_count": 0},
        }

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print") as mock_print:
                        with pytest.raises(SystemExit) as exc_info:
                            main(["list", "--format", "json"])
                        assert exc_info.value.code == 0
                        printed = mock_print.call_args[0][0]
                        data = json.loads(printed)
                        assert data["ok"] is True


class TestMainView:
    def test_view_with_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_task_detail.return_value = {"description": "Do this"}

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(
                                [
                                    "view",
                                    "https://bj80.managebac.cn/student/classes/11460711/core_tasks/27254393",
                                    "--format",
                                    "json",
                                ]
                            )
                        assert exc_info.value.code == 0

    def test_view_missing_target(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["view", "--format", "json"])
                        assert exc_info.value.code == 1

    def test_view_task_not_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = {
            "upcoming": [],
            "past": [],
            "overdue": [],
            "student_name": "X",
            "school": "s",
            "base_url": "u",
            "crawled_at": "t",
        }

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["view", "99999", "--format", "json"])
                    assert exc_info.value.code == 1


class TestMainSubmit:
    def test_submit_missing_target(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["submit", "--format", "json"])
                    assert exc_info.value.code == 1

    def test_submit_missing_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with pytest.raises(SystemExit) as exc_info:
                        main(["submit", "12345", "--format", "json"])
                    assert exc_info.value.code == 1


class TestMainLogout:
    def test_logout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        with pytest.raises(SystemExit) as exc_info:
            main(["logout", "--format", "json"])
        assert exc_info.value.code == 0


class TestMainCalendar:
    def test_calendar_today(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_calendar_events.return_value = []

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["calendar", "--today", "--format", "json"])
                        assert exc_info.value.code == 0


class TestMainTimetable:
    def test_timetable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_timetable.return_value = {"days": [], "lessons": []}

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["timetable", "--format", "json"])
                        assert exc_info.value.code == 0


class TestMainGrades:
    def test_grades_list_classes(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.crawl_all.return_value = {
            "upcoming": [{"class_name": "Math", "link": "/student/classes/100/c/1"}],
            "past": [],
            "overdue": [],
            "student_name": "X",
            "school": "s",
            "base_url": "u",
            "crawled_at": "t",
        }

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["grades", "--format", "json"])
                        assert exc_info.value.code == 0

    def test_grades_with_class_id(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_class_grades.return_value = {
            "tasks": [],
            "categories": [],
            "grade_scale": {},
            "expected_grade": None,
        }

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["grades", "--class-id", "100", "--format", "json"])
                        assert exc_info.value.code == 0


class TestMainNotifications:
    def test_notifications_list(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_notification_token.return_value = ("endpoint", "token")

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("mb_cli.__main__.MNNHubClient") as MockHub:
                        mock_hub = MockHub.return_value
                        mock_hub.stats.return_value = {"unread_messages": 2}
                        mock_hub.list.return_value = {
                            "items": [{"id": 1, "title": "Test", "is_read": True}],
                            "meta": {"page": 1},
                        }
                        with patch("builtins.print"):
                            with pytest.raises(SystemExit) as exc_info:
                                main(["notifications", "--format", "json"])
                            assert exc_info.value.code == 0

    def test_notifications_read(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.get_notification_token.return_value = ("ep", "tok")

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("mb_cli.__main__.MNNHubClient") as MockHub:
                        mock_hub = MockHub.return_value
                        mock_hub.mark_read.return_value = True
                        with patch("builtins.print"):
                            with pytest.raises(SystemExit) as exc_info:
                                main(
                                    [
                                        "notifications",
                                        "--read",
                                        "12345",
                                        "--format",
                                        "json",
                                    ]
                                )
                            assert exc_info.value.code == 0


class TestMainDaemon:
    def test_daemon_stop(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        with patch("mb_cli.__main__.stop_daemon") as mock_stop:
            mock_stop.return_value = {"stopped": False, "reason": "pid_file_missing"}
            with patch("builtins.print"):
                with pytest.raises(SystemExit) as exc_info:
                    main(["daemon", "stop", "--format", "json"])
                assert exc_info.value.code == 0

    def test_daemon_configure_webhook(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        with patch("mb_cli.__main__.configure_webhook") as mock_conf:
            mock_conf.return_value = {"webhook_url": "http://new:8080/hook"}
            with patch("builtins.print"):
                with pytest.raises(SystemExit) as exc_info:
                    main(
                        [
                            "daemon",
                            "configure-webhook",
                            "http://new:8080/hook",
                            "--format",
                            "json",
                        ]
                    )
                assert exc_info.value.code == 0


class TestMainCountGradeFreq:
    def test_count_grade_freq(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MB_CRAWLER_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setenv("MB_CRAWLER_SESSION", str(tmp_path / "session.json"))

        mock_client = MagicMock()
        mock_client.count_grade_frequencies.return_value = {
            "grades": {"A": 3, "B": 2},
            "total": 5,
            "classes": [{"id": "100", "name": "Math"}],
        }

        with patch("mb_cli.__main__._build_client") as mock_bc:
            mock_bc.return_value = _mock_build_client_result(mock_client)
            with patch("mb_cli.__main__.save_profile"):
                with patch("mb_cli.__main__.save_session"):
                    with patch("builtins.print"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["count-grade-freq", "--format", "json"])
                        assert exc_info.value.code == 0
