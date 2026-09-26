"""Interactive repository/task collection for ``harness run``.

A single ``Enter`` submits a one-line task, including a GitHub issue URL, with
no need to "finish with an empty line". Typing ``:multi`` and pressing Enter
switches to an explicit multiline mode (blank line ends it) for longer task
descriptions. This works identically whether stdin is a real terminal or a
plain pipe, so it stays deterministic and unit-testable without a PTY.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

MULTILINE_SENTINEL = ":multi"


class InputError(Exception):
    """Invalid or absent input. The message is safe to print."""


def prompt_line(label: str, stdin: TextIO, stdout: TextIO, *, arrow: str = "›", default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    stdout.write(f"{label}{hint} {arrow} ")
    stdout.flush()
    line = stdin.readline()
    if not line:
        stdout.write("\n")
        raise InputError(f"no input received for '{label}'")
    text = line.strip()
    return text or default


def prompt_task(stdin: TextIO, stdout: TextIO, *, arrow: str = "›") -> str:
    """Reads the task. One line submits immediately; ``:multi`` switches to
    explicit multiline collection."""
    stdout.write(f"Task {arrow} ")
    stdout.flush()
    line = stdin.readline()
    if not line:
        stdout.write("\n")
        raise InputError("no input received for 'Task'")
    text = line.rstrip("\n").strip()
    if text == MULTILINE_SENTINEL:
        return prompt_multiline(stdin, stdout)
    return text


def prompt_multiline(stdin: TextIO, stdout: TextIO) -> str:
    stdout.write("Multiline task — finish with a blank line:\n")
    stdout.flush()
    lines = []
    while True:
        line = stdin.readline()
        if not line or not line.strip():
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def boxed_prompt(label: str, stdin: TextIO, stdout: TextIO, theme, *, hint: str = "", default: str = "") -> str:
    """TTY-only: an input box (Gemini-CLI style). The open box is drawn while typing;
    after Enter it is redrawn closed and dimmed, showing the accepted value."""
    from harness.ui.panel import bottom_border, panel, top_border
    from harness.ui.theme import CLEAR_TO_END, cursor_up

    width = theme.panel_width
    stdout.write(top_border(theme, width, label, "accent", hint) + "\n")
    prefix = theme.paint(theme.box("v"), "accent") + " " + theme.paint(theme.arrow, "accent") + " "
    stdout.write(prefix)
    stdout.flush()
    line = stdin.readline()
    if not line:
        stdout.write("\n" + bottom_border(theme, width) + "\n")
        raise InputError(f"no input received for '{label}'")
    typed = line.rstrip("\n")
    value = typed.strip() or default
    rows = max(1, -(-(4 + len(typed)) // max(theme.width, 1)))
    stdout.write(cursor_up(rows + 1) + "\r" + CLEAR_TO_END)
    shown = value if value.strip() != MULTILINE_SENTINEL else "(multiline)"
    stdout.write("\n".join(panel(theme, [[(theme.arrow + " ", "dim"), (shown, None)]], title=label,
                                 border="dim", width=width)) + "\n")
    stdout.flush()
    return value


def boxed_task(stdin: TextIO, stdout: TextIO, theme) -> str:
    text = boxed_prompt("Task", stdin, stdout, theme, hint="issue URL or description · :multi for more lines")
    if text.strip() != MULTILINE_SENTINEL:
        return text.strip()
    from harness.ui.panel import bottom_border, top_border
    width = theme.panel_width
    stdout.write(top_border(theme, width, "Task", "accent", "blank line to finish") + "\n")
    rail = theme.paint(theme.box("v"), "accent") + " "
    lines = []
    while True:
        stdout.write(rail)
        stdout.flush()
        line = stdin.readline()
        if not line or not line.strip():
            break
        lines.append(line.rstrip("\n"))
    if not line:
        stdout.write("\n")
    stdout.write(bottom_border(theme, width) + "\n")
    stdout.flush()
    return "\n".join(lines)


def default_repository(cwd: Path) -> str:
    """``.`` when the current directory looks usable as the default repository hint."""
    try:
        return "." if cwd.is_dir() else ""
    except OSError:
        return ""
