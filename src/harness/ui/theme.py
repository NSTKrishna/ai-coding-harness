"""Terminal capability detection and the status symbol/color vocabulary.

Respects ``NO_COLOR`` (https://no-color.org), ``TERM=dumb``, non-TTY streams
and non-UTF-8 encodings. Every symbol has an ASCII fallback so the status
vocabulary stays understandable in monochrome/limited terminals; nothing here
is conveyed by color alone.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Mapping, Optional, TextIO

_UNICODE_SYMBOLS = {
    "pending": "○",
    "active": "◐",
    "success": "✓",
    "failure": "✕",
    "warning": "!",
    "skipped": "—",
    "info": "●",
}
_ASCII_SYMBOLS = {
    "pending": "o",
    "active": "...",
    "success": "OK",
    "failure": "FAIL",
    "warning": "WARN",
    "skipped": "-",
    "info": "*",
}

ARROW_UNICODE = "›"   # ›
ARROW_ASCII = ">"
STEP_UNICODE = "→"    # →
STEP_ASCII = "->"
BULLET_UNICODE = "·"  # ·
BULLET_ASCII = "*"


def is_tty(stream: Optional[TextIO]) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except ValueError:   # closed stream
        return False


def supports_unicode(stream: Optional[TextIO]) -> bool:
    encoding = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in encoding


def color_enabled(stream: Optional[TextIO], environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False
    return is_tty(stream)


def animation_enabled(stream: Optional[TextIO], environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether cursor movement / in-place redraw is safe to use."""
    env = os.environ if environ is None else environ
    if env.get("TERM") == "dumb":
        return False
    return is_tty(stream)


def terminal_width(environ: Optional[Mapping[str, str]] = None, default: int = 80) -> int:
    env = os.environ if environ is None else environ
    columns = env.get("COLUMNS")
    if columns:
        try:
            value = int(columns)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


# 256-color palette (Gemini-CLI-like: blue -> purple -> pink accents on a neutral base).
_PAINT = {
    "accent": "38;5;75",     # sky blue
    "accent2": "38;5;141",   # soft purple
    "dim": "38;5;245",       # secondary text (still readable, not low-contrast black)
    "bold": "1",
    "green": "38;5;78",
    "yellow": "38;5;221",
    "red": "38;5;203",
    "cyan": "38;5;81",
}
_GRADIENT_256 = (69, 75, 111, 105, 141, 140, 176, 175)
_GRADIENT_RGB = ((71, 150, 228), (132, 122, 206), (195, 103, 127))   # Gemini's blue -> purple -> rose
_KIND_PAINT = {"pending": "dim", "active": "accent", "success": "green", "failure": "red",
               "warning": "yellow", "skipped": "dim", "info": "accent2"}

_BOX_UNICODE = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
_BOX_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}
SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else (text[: width - 1] + "…" if width > 1 else text[:width])


@dataclass(frozen=True)
class Theme:
    color: bool = False
    unicode: bool = False
    width: int = 80
    truecolor: bool = False

    def symbol(self, kind: str) -> str:
        table = _UNICODE_SYMBOLS if self.unicode else _ASCII_SYMBOLS
        return table.get(kind, table["info"])

    def style(self, text: str, kind: str) -> str:
        return self.paint(text, _KIND_PAINT.get(kind, "dim"))

    def paint(self, text: str, name: str) -> str:
        if not self.color or not text:
            return text
        return f"\x1b[{_PAINT.get(name, '0')}m{text}\x1b[0m"

    def gradient(self, text: str) -> str:
        """Left-to-right color gradient (banner/brand only). Plain text without color."""
        if not self.color:
            return text
        n = max(len(text) - 1, 1)
        out = []
        for i, ch in enumerate(text):
            if ch == " ":
                out.append(ch)
                continue
            t = i / n
            if self.truecolor:
                seg = min(int(t * (len(_GRADIENT_RGB) - 1)), len(_GRADIENT_RGB) - 2)
                local = t * (len(_GRADIENT_RGB) - 1) - seg
                a, b = _GRADIENT_RGB[seg], _GRADIENT_RGB[seg + 1]
                r, g, bl = (round(a[k] + (b[k] - a[k]) * local) for k in range(3))
                out.append(f"\x1b[38;2;{r};{g};{bl}m{ch}")
            else:
                out.append(f"\x1b[38;5;{_GRADIENT_256[min(int(t * len(_GRADIENT_256)), len(_GRADIENT_256) - 1)]}m{ch}")
        return "".join(out) + "\x1b[0m"

    def box(self, part: str) -> str:
        return (_BOX_UNICODE if self.unicode else _BOX_ASCII)[part]

    @property
    def spinner(self) -> str:
        return SPINNER_UNICODE if self.unicode else SPINNER_ASCII

    @property
    def panel_width(self) -> int:
        return max(min(self.width, 100) - 2, 30)

    @property
    def arrow(self) -> str:
        return ARROW_UNICODE if self.unicode else ARROW_ASCII

    @property
    def step_sep(self) -> str:
        return STEP_UNICODE if self.unicode else STEP_ASCII

    @property
    def bullet(self) -> str:
        return BULLET_UNICODE if self.unicode else BULLET_ASCII

    @classmethod
    def detect(cls, stream: Optional[TextIO], environ: Optional[Mapping[str, str]] = None) -> "Theme":
        env = os.environ if environ is None else environ
        return cls(color=color_enabled(stream, environ), unicode=supports_unicode(stream),
                   width=terminal_width(environ),
                   truecolor=(env.get("COLORTERM") or "").lower() in ("truecolor", "24bit"))


def cursor_up(n: int) -> str:
    return f"\x1b[{n}A" if n > 0 else ""


CLEAR_TO_END = "\x1b[0J"
