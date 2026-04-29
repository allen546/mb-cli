"""Tests for mb_crawler.exceptions."""

from mb_crawler.exceptions import CommandError


class TestCommandError:
    def test_has_code_and_message(self):
        exc = CommandError("missing_credentials", "Missing email")
        assert exc.code == "missing_credentials"
        assert exc.message == "Missing email"

    def test_is_exception(self):
        exc = CommandError("code", "msg")
        assert isinstance(exc, Exception)

    def test_str_representation(self):
        exc = CommandError("code", "Something went wrong")
        assert str(exc) == "Something went wrong"

    def test_can_be_caught_as_exception(self):
        try:
            raise CommandError("test", "test msg")
        except Exception as e:
            assert isinstance(e, CommandError)
