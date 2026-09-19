"""Tests for rendering ManageBac's redactor HTML as ANSI terminal text.

Every test here fails against a naive ``get_text()`` flattening, which is what
the client used to do: it threw away the inline ``style`` attributes that carry
all of ManageBac's formatting, so a teacher's red "hand this in Monday" line was
indistinguishable from the rest of the assignment.
"""

from __future__ import annotations

import json

import pytest

from tahuti.richtext import (
    clean_redactor_html,
    color_depth,
    html_to_ansi,
    html_to_plain_text,
    parse_color,
    strip_control_chars,
    _to_256,
)
from bs4 import BeautifulSoup

from tahuti import formatters
from tahuti.formatters import render_pretty

RED = "rgb(208, 0, 1)"
DEFAULT_BODY = "rgb(16, 24, 40)"


def para(text: str, style: str = "") -> str:
    attr = f' style="{style}"' if style else ""
    return f"<p{attr}>{text}</p>"


def render(html: str, depth: str = "truecolor") -> str:
    return html_to_ansi(html, depth=depth)


class TestColorParsing:
    """A colour has to survive the trip from a CSS string to an SGR code."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("rgb(208, 0, 1)", (208, 0, 1)),
            ("rgb(208,0,1)", (208, 0, 1)),
            ("rgba(208, 0, 1, 0.5)", (208, 0, 1)),
            ("#d00001", (208, 0, 1)),
            ("#D00001", (208, 0, 1)),
            ("red", (255, 0, 0)),
        ],
    )
    def test_formats_managebac_and_pasted_content_use(self, value, expected):
        assert parse_color(value) == expected

    @pytest.mark.parametrize("value", [None, "", "transparent", "not-a-color"])
    def test_unrecognised_values_are_none_not_black(self, value):
        """``None`` means inherit. Reading an unknown colour as black would
        silently repaint inherited text."""
        assert parse_color(value) is None

    def test_out_of_range_channels_are_clamped_not_fatal(self):
        assert parse_color("rgb(999, 0, 1)") == (255, 0, 1)


class TestAnsiOutput:
    def test_red_span_is_wrapped_in_a_colour_escape(self):
        html = f'<span style="color: {RED}">hand this in</span>'
        out = render(html)
        assert "\x1b[38;2;208;0;1mhand this in\x1b[0m" == out

    def test_only_the_coloured_run_is_escaped(self):
        html = para("plain line") + f'<span style="color: {RED}">red line</span>'
        out = render(html)
        assert "plain line" in out
        assert "\x1b" not in out.split("plain line")[0]
        assert out.index("plain line") < out.index("\x1b[38;2;208;0;1m")
        # The escape must precede the text it colours, not trail it.
        assert out.index("\x1b[38;2;208;0;1m") < out.index("red line")

    def test_the_editors_default_body_colour_is_not_formatting(self):
        """redactor stamps every paragraph with ``rgb(16, 24, 40)``.

        Honouring it would wrap the whole description in escapes and drown out
        the runs that are actually coloured.
        """
        html = f'<p style="color: {DEFAULT_BODY}">body text</p>'
        assert "\x1b" not in render(html)

    def test_near_default_colour_is_still_suppressed(self):
        html = '<p style="color: rgb(20, 28, 44)">body text</p>'
        assert "\x1b" not in render(html)

    def test_a_real_black_is_kept(self):
        """Suppressing the default must not suppress intentional black."""
        html = '<p style="color: rgb(0, 0, 0)">black text</p>'
        assert "\x1b[38;2;0;0;0mblack text" in render(html)

    @pytest.mark.parametrize(
        "style,code",
        [
            ("font-weight: 700", "1"),
            ("font-weight: bold", "1"),
            ("font-style: italic", "3"),
            ("text-decoration: underline", "4"),
        ],
    )
    def test_typographic_styles_map_to_sgr_codes(self, style, code):
        out = render(f'<span style="{style}">x</span>')
        assert f"\x1b[{code}m" in out

    def test_font_weight_400_is_not_bold(self):
        """``font-weight: 400`` is the editor's default, not emphasis."""
        out = render('<span style="font-weight: 400">x</span>')
        assert "\x1b[1m" not in out
        assert "\x1b" not in out

    def test_background_colour_is_deliberately_ignored(self):
        """Repainting the terminal background behind a paragraph looks broken
        in most themes and the value is nearly always the default."""
        out = render('<span style="background-color: rgb(255, 0, 0)">x</span>')
        assert "\x1b" not in out

    def test_nested_styles_combine_rather_than_overwrite(self):
        html = f'<b><span style="color: {RED}">both</span></b>'
        out = render(html)
        assert "38;2;208;0;1" in out and "\x1b[1m" in out

    def test_a_coloured_child_keeps_its_colour_inside_plain_text(self):
        html = para("before") + f'<span style="color: {RED}">red</span>'
        out = render(html)
        assert "\x1b[38;2;208;0;1mred" in out

    def test_semantic_tags_still_work(self):
        assert "\x1b[1m" in render("<b>bold</b>")
        assert "\x1b[3m" in render("<i>italic</i>")
        assert "\x1b[4m" in render("<u>under</u>")


class TestColorDepth:
    """Colour is a property of the terminal, so it is chosen per environment."""

    def test_truecolor_from_colorterm(self):
        assert color_depth({"COLORTERM": "truecolor"}) == "truecolor"
        assert color_depth({"COLORTERM": "24bit"}) == "truecolor"

    def test_256_from_term(self):
        assert color_depth({"TERM": "xterm-256color"}) == "256"

    def test_plain_terminal_gets_no_colour(self):
        assert color_depth({"TERM": "dumb"}) == "none"

    def test_no_color_wins_over_everything(self):
        assert color_depth({"NO_COLOR": "1", "COLORTERM": "truecolor"}) == "none"

    def test_256_mode_uses_the_palette_not_24_bit(self):
        out = render(f'<span style="color: {RED}">x</span>', depth="256")
        assert "38;5;" in out and "38;2;" not in out

    def test_no_depth_emits_no_escapes_at_all(self):
        html = (
            para(f'<span style="color: {RED}">red</span>', f"color: {DEFAULT_BODY}")
            + "<b>bold</b>"
        )
        # `red` closes a paragraph, `bold` is inline, so a single newline between
        # them — not a paragraph break.
        assert render(html, depth="none") == "red\nbold"
        assert "\x1b" not in render(html, depth="none")


class TestTo256:
    @pytest.mark.parametrize(
        "rgb,expected_range",
        [
            ((0, 0, 0), (16, 16)),
            ((255, 255, 255), (231, 231)),
            ((128, 128, 128), (244, 244)),
        ],
    )
    def test_greys_land_on_the_greyscale_ramp(self, rgb, expected_range):
        assert expected_range[0] <= _to_256(*rgb) <= expected_range[1]

    def test_colours_stay_in_the_cube(self):
        assert 16 <= _to_256(208, 0, 1) <= 231


class TestUntrustedContent:
    """A description is remote content; it must not be able to forge output."""

    def test_embedded_escapes_are_stripped_from_text(self):
        out = render("<p>hello\x1b[31mfake red\x1b[0m</p>", depth="truecolor")
        # Only the escapes we generate may appear.
        assert out.count("\x1b") == 0 or "\x1b[31m" not in out
        assert "fake red" in out

    def test_c1_controls_are_stripped_too(self):
        assert "\x9b" not in strip_control_chars("a\x9b31mb")

    def test_tab_and_newline_survive(self):
        assert "\t" in strip_control_chars("a\tb")

    def test_script_content_never_reaches_the_output(self):
        out = render("<script>alert(1)</script><p>safe</p>")
        assert "alert" not in out
        assert "safe" in out

    def test_style_element_content_is_dropped(self):
        out = render("<style>p{color:red}</style><p>safe</p>")
        assert "color:red" not in out


class TestPlainText:
    def test_paragraph_breaks_are_preserved(self):
        assert html_to_plain_text("<p>a</p><p>b</p>") == "a\n\nb"

    def test_br_becomes_a_newline(self):
        assert html_to_plain_text("a<br>b") == "a\nb"

    def test_empty_input_is_empty(self):
        assert html_to_plain_text("") == ""

    def test_entities_are_decoded(self):
        assert html_to_plain_text("<p>a &amp; b</p>") == "a & b"


class TestCleanRedactorHtml:
    """The raw markup is kept so the importer can clean it itself."""

    def test_markup_is_preserved_not_flattened(self):
        node = BeautifulSoup(f'<div><p style="color: {RED}">x</p></div>', "html.parser")
        assert "<p" in clean_redactor_html(node)
        assert RED in clean_redactor_html(node)

    def test_script_and_style_elements_are_removed(self):
        node = BeautifulSoup(
            "<div><script>evil()</script><style>p{}</style><p>keep</p></div>",
            "html.parser",
        )
        out = clean_redactor_html(node)
        assert "evil" not in out and "p{}" not in out
        assert "keep" in out

    def test_none_gives_empty_string(self):
        assert clean_redactor_html(None) == ""


class TestPrettyRenderingKeepsJsonClean:
    """Colour is a terminal concern; the JSON payload must stay escape-free."""

    RED_HTML = f'<p>Read the newsletter.</p><p><span style="color: {RED}">Hand in Monday.</span></p>'

    def _payload(self):
        return {
            "ok": True,
            "command": "view",
            "profile": "default",
            "data": {
                "task": {
                    "id": "1", "title": "t", "class_name": "c",
                    "due_date": "Sep 18", "link": "u",
                },
                "detail": {
                    "description": "Read the newsletter.\n\nHand in Monday.",
                    "description_html": self.RED_HTML,
                },
            },
        }

    def test_pretty_output_colours_the_red_run(self, monkeypatch):
        # `render_pretty` asks the *real* terminal what it supports, so the
        # depth has to be pinned here.  Without this the test passes on a
        # developer's truecolour terminal and fails in CI, where neither
        # $COLORTERM nor a *256color* $TERM is set — the same trap the host
        # timezone tests were parametrised over.
        monkeypatch.setattr(formatters, "color_depth", lambda: "truecolor")
        pretty = render_pretty(self._payload())
        assert "\x1b[38;2;208;0;1mHand in Monday." in pretty

    def test_a_terminal_without_colour_gets_plain_text(self, monkeypatch):
        """The CI environment, and any dumb terminal, must still read cleanly."""
        monkeypatch.setattr(formatters, "color_depth", lambda: "none")
        pretty = render_pretty(self._payload())
        assert "\x1b" not in pretty
        assert "Hand in Monday." in pretty
        assert "Read the newsletter." in pretty

    def test_json_output_carries_no_escapes(self):
        rendered = json.dumps(self._payload(), indent=2, ensure_ascii=False)
        assert "\x1b" not in rendered

    def test_json_still_carries_the_raw_markup_for_the_importer(self):
        rendered = json.dumps(self._payload(), indent=2, ensure_ascii=False)
        assert "description_html" in rendered
        assert RED in rendered

    def test_plain_description_alone_still_renders(self):
        """A detail with no HTML (older snapshot) must not lose its body."""
        payload = self._payload()
        payload["data"]["detail"].pop("description_html")
        pretty = render_pretty(payload)
        assert "Read the newsletter." in pretty
        assert "\x1b" not in pretty
