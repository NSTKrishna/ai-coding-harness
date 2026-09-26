"""Rounded panels and the brand banner for the interactive terminal UI.

Rows are lists of ``(text, paint)`` segments so width is computed on plain
text (never on escape codes) and truncation never cuts an escape sequence.
"""

from __future__ import annotations

import textwrap
from typing import Iterable, Optional, Sequence

from harness.ui.theme import Theme, truncate

Segment = tuple[str, Optional[str]]
Row = Sequence[Segment]

LOGO = (
    "██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗",
    "██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝",
    "███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗",
    "██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║",
    "██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║",
    "╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝",
)

TIPS = (
    "Paste a GitHub issue URL or describe a task — Enter submits.",
    "Type :multi for a longer, multi-line task.",
    "Nothing is called done until tests prove it: VERIFIED needs evidence.",
)


def banner(theme: Theme, version: str = "") -> str:
    lines = [""]
    if theme.unicode and theme.width >= len(LOGO[0]) + 2:
        lines += [" " + theme.gradient(row) for row in LOGO]
    else:
        lines.append(" " + theme.gradient("HARNESS"))
    tagline = "autonomous software engineering · verified changes only" if theme.unicode \
        else "autonomous software engineering - verified changes only"
    lines += ["", " " + theme.paint(tagline + (f"  v{version}" if version else ""), "dim"), ""]
    lines.append(" " + theme.paint("Tips for getting started:", "bold"))
    lines += [" " + theme.paint(f"{i}. ", "dim") + tip for i, tip in enumerate(TIPS, 1)]
    return "\n".join(lines) + "\n\n"


def render_row(theme: Theme, row: Row, width: int) -> str:
    out, used = [], 0
    for text, paint in row:
        room = width - used
        if room <= 0:
            break
        text = truncate(text, room)
        out.append(theme.paint(text, paint) if paint else text)
        used += len(text)
    return "".join(out) + " " * max(width - used, 0)


def top_border(theme: Theme, width: int, title: str = "", border: str = "accent", hint: str = "") -> str:
    h = theme.box("h")
    title_part = f" {title} " if title else ""
    hint_part = f" {hint} " if hint else ""
    fill = max(width - 3 - len(title_part) - len(hint_part), 0)
    return (theme.paint(theme.box("tl") + h, border) + theme.paint(title_part, "bold")
            + theme.paint(hint_part, "dim") + theme.paint(h * fill + theme.box("tr"), border))


def bottom_border(theme: Theme, width: int, border: str = "accent") -> str:
    return theme.paint(theme.box("bl") + theme.box("h") * (width - 2) + theme.box("br"), border)


def panel(theme: Theme, rows: Iterable[Row], *, title: str = "", border: str = "accent",
          width: Optional[int] = None) -> list[str]:
    w = width or theme.panel_width
    inner = w - 4
    v = theme.paint(theme.box("v"), border)
    lines = [top_border(theme, w, title, border)]
    lines += [f"{v} {render_row(theme, row, inner)} {v}" for row in rows]
    lines.append(bottom_border(theme, w, border))
    return lines


def wrapped(text: str, width: int, paint: Optional[str] = None, indent: str = "") -> list[list[Segment]]:
    """Rows for a paragraph, wrapped to ``width`` (content width, after ``indent``)."""
    rows: list[list[Segment]] = []
    for para in text.splitlines() or [""]:
        chunks = textwrap.wrap(para, max(width - len(indent), 10)) or [""]
        rows += [[(indent + chunk, paint)] for chunk in chunks]
    return rows
