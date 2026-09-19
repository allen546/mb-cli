"""Render ManageBac's redactor HTML as ANSI-coloured terminal text.

ManageBac's task descriptions are WYSIWYG (redactor) output: formatting arrives
as inline ``style`` attributes — ``color: rgb(208, 0, 1)``, ``font-weight``,
``text-decoration`` — never as semantic tags.  Flattening that to plain text
throws away meaning the user can see in the browser, so this module converts it
to ANSI escapes instead.

Two rules shape the design:

* **Never flatten at the source.**  ``get_task_detail`` keeps the raw HTML in
  ``description_html`` alongside the plain ``description``, so the importer can
  clean ManageBac markup into ``MBEvent`` itself rather than receiving a lossy
  string.  ANSI is a *presentation* of the same content, not a replacement.

* **We generate every escape.**  Descriptions are remote content, and a raw
  ``ESC`` embedded in one would let it forge output or drive the terminal, so
  text nodes are stripped of control characters before they are emitted.

Background colour is deliberately ignored: repainting the terminal background
behind a paragraph looks broken in most themes and the information is almost
always the default.
"""

from __future__ import annotations

import os
import re

# Control characters, minus tab/newline/carriage-return which are handled as
# whitespace by the block walker.  C0 (except the three above), DEL, and the
# C1 range all go: any of them could otherwise smuggle an escape in.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")

# The handful of CSS colours ManageBac's editor actually offers, plus the ones
# that show up when a teacher pastes from Word.
_NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}

# Elements that end a line.  ``<br>`` is handled separately because it carries
# no closing tag.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "blockquote", "pre", "section", "article",
}


def strip_control_chars(text: str) -> str:
    """Remove characters that could forge terminal output.

    Only the escape and control ranges go; ordinary text, including CJK and
    zero-width spaces used by the editor, is left untouched.
    """
    return _CONTROL_CHARS.sub("", text)


def color_depth(env: dict | None = None) -> str:
    """``"truecolor"``, ``"256"`` or ``"none"`` for the current terminal.

    ``NO_COLOR`` (any non-empty value) wins outright, per the convention.
    """
    environ = env if env is not None else os.environ
    if environ.get("NO_COLOR"):
        return "none"
    colorterm = (environ.get("COLORTERM") or "").lower()
    if colorterm in {"truecolor", "24bit"}:
        return "truecolor"
    if "256color" in (environ.get("TERM") or "").lower():
        return "256"
    return "none"


def _to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 palette index for an RGB triple."""
    if r == g == b:
        # Grayscale ramp 232-255 steps through 238,240,... in 8-unit jumps;
        # below 8 the first 16 colours are a better fit than the ramp.
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def parse_color(value: str | None) -> tuple[int, int, int] | None:
    """Parse a CSS colour to RGB, or ``None`` if it is not one we understand."""
    if not value:
        return None
    text = value.strip().lower()
    match = _RGB.search(text)
    if match:
        r, g, b = (int(x) for x in match.groups())
        # Clamp: an out-of-range component is a malformed style, not a crash.
        return (min(r, 255), min(g, 255), min(b, 255))
    match = _HEX.search(text)
    if match:
        digits = match.group(1)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    if text in _NAMED_COLORS:
        return _NAMED_COLORS[text]
    return None


# ManageBac's redactor stamps *every* paragraph with the body colour
# ``rgb(16, 24, 40)``.  That is not formatting — it is what unstyled text looks
# like — so honouring it would wrap the entire description in SGR sequences and
# drown out the runs that are genuinely coloured.
_DEFAULT_TEXT_COLOR = (16, 24, 40)
_DEFAULT_COLOR_TOLERANCE = 24


def _is_default_text_color(color: tuple[int, int, int] | None) -> bool:
    return color is not None and all(
        abs(channel - default) <= _DEFAULT_COLOR_TOLERANCE
        for channel, default in zip(color, _DEFAULT_TEXT_COLOR)
    )


def _style_of(style: str) -> tuple[tuple[int, int, int] | None, bool, bool, bool]:
    """Extract ``(color, bold, italic, underline)`` from a ``style`` attribute."""
    declarations = {}
    for part in style.split(";"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        declarations[key.strip().lower()] = value.strip().lower()

    color = parse_color(declarations.get("color"))
    if _is_default_text_color(color):
        color = None

    # `font-weight: 400` is the editor's default and must not read as bold;
    # only >=600 or the literal keyword does.
    weight = declarations.get("font-weight", "")
    bold = weight in {"bold", "bolder"} or (weight.isdigit() and int(weight) >= 600)

    italic = declarations.get("font-style", "") == "italic"

    decoration = declarations.get("text-decoration", "") + " " + declarations.get(
        "text-decoration-line", ""
    )
    underline = "underline" in decoration

    return color, bold, italic, underline


def _codes_for(state: tuple[tuple[int, int, int] | None, bool, bool, bool], depth: str) -> list[str]:
    """SGR parameters for *state*, or none when the terminal takes no styling.

    ``depth="none"`` suppresses bold and underline along with colour: the
    contract is that "none" means plain text with no escapes whatsoever, which
    is what :func:`html_to_plain_text` and ``$NO_COLOR`` both promise.
    """
    if depth == "none":
        return []
    color, bold, italic, underline = state
    codes: list[str] = []
    if color is not None:
        if depth == "truecolor":
            codes.append(f"38;2;{color[0]};{color[1]};{color[2]}")
        elif depth == "256":
            codes.append(f"38;5;{_to_256(*color)}")
    if bold:
        codes.append("1")
    if italic:
        codes.append("3")
    if underline:
        codes.append("4")
    return codes


_NEUTRAL = (None, False, False, False)


def _merge(child: tuple, parent: tuple) -> tuple:
    """Inherit a child's style from its parent, CSS-fashion.

    Every property except colour is inherited, so a ``<b>`` wrapping coloured
    text keeps its colour.  A colour of ``None`` means "inherit", never "black".
    """
    color, bold, italic, underline = child
    p_color, p_bold, p_italic, p_underline = parent
    return (
        color or p_color,
        bold or p_bold,
        italic or p_italic,
        underline or p_underline,
    )


def clean_redactor_html(node) -> str:
    """A node's inner HTML with ``<script>``/``<style>`` removed, otherwise verbatim.

    Stored alongside the flattened text so the importer can do its own
    ManageBac -> ``MBEvent`` cleaning from the real markup instead of a string
    this module already threw structure away on.  Only the two elements that
    never render are dropped; no rewriting or normalising happens here, because
    that is the importer's job, not the scraper's.
    """
    if node is None:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(node), "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    body = soup.body
    children = list(body.children) if body is not None else list(soup.children)
    return "".join(str(child) for child in children).strip()


def html_to_ansi(html: str, *, depth: str = "none") -> str:
    """Convert redactor HTML to text carrying ANSI styling.

    ``depth`` selects the colour encoding (see :func:`color_depth`).  At
    ``"none"`` the markup is flattened to plain text, which is what makes this
    safe to call unconditionally: the caller just passes whatever the terminal
    supports.
    """
    if not html:
        return ""

    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    current: tuple = _NEUTRAL
    active = False  # is an SGR sequence currently in effect?

    def apply(state: tuple) -> None:
        """Switch the terminal to *state*, resetting only when one is open.

        At ``depth="none"`` every style maps to no codes, so ``active`` never
        becomes true and not a single escape is emitted — which is what makes it
        safe to call this unconditionally and let the caller pick the depth.
        """
        nonlocal current, active
        if state == current:
            return
        if active:
            out.append("\x1b[0m")
            active = False
        codes = _codes_for(state, depth)
        if codes:
            out.append(f"\x1b[{';'.join(codes)}m")
            active = True
        current = state

    def emit(text: str) -> None:
        nonlocal active
        if not text:
            return
        if not active:
            codes = _codes_for(current, depth)
            if codes:
                out.append(f"\x1b[{';'.join(codes)}m")
                active = True
        out.append(text)

    def walk(node: Tag | NavigableString, style: tuple) -> None:
        if isinstance(node, NavigableString):
            emit(strip_control_chars(str(node)))
            return
        if not isinstance(node, Tag):
            return

        own = style
        if node.name in {"b", "strong"}:
            own = (own[0], True, own[2], own[3])
        elif node.name in {"i", "em"}:
            own = (own[0], own[1], True, own[3])
        elif node.name in {"u", "ins"}:
            own = (own[0], own[1], own[2], True)
        elif node.name in {"style", "script"}:
            return  # never rendered, and must never reach the terminal
        elif node.get("style"):
            own = _merge(_style_of(node["style"]), own)

        if node.name == "br":
            out.append("\n")
            return

        # The newline comes before the style opens, so the escape sits
        # immediately in front of the text it colours rather than in front of a
        # blank line.
        is_block = node.name in _BLOCK_TAGS
        if is_block:
            out.append("\n")
        # The style has to be opened *before* the children's text and closed
        # back to the parent's afterwards; doing it the other way round puts the
        # escape at the end of the run it was meant to colour.
        apply(own)
        for child in node.children:
            walk(child, own)
        apply(style)
        if is_block:
            out.append("\n")

    for child in soup.children:
        walk(child, _NEUTRAL)

    if active:
        out.append("\x1b[0m")

    text = "".join(out)
    # Collapse the blank lines block tags produce, and trim the edges.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def html_to_plain_text(html: str) -> str:
    """Flatten redactor HTML to plain text, keeping paragraph breaks."""
    return html_to_ansi(html, depth="none")
