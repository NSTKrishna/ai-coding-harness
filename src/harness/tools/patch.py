"""apply_patch: a pure-Python unified-diff applier (no ``patch`` binary, no git needed).

Supported: modifying, creating (``--- /dev/null``) and deleting (``+++ /dev/null``)
UTF-8 text files, several files and hunks per patch, ``a/``/``b/`` prefixes,
``\\ No newline at end of file`` markers.

Not supported (rejected with a clear error): renames, binary patches, mode-only
changes.

Matching rules:

- Hunk line counts in ``@@`` headers are not trusted (models get them wrong);
  hunk bodies are read by line prefix.
- Each hunk's old lines (context + removals) must match the file exactly. The
  stated line number is only a hint: the match closest to it wins, searching
  forward from the previous hunk.
- If no exact match exists, a match that ignores trailing whitespace is
  accepted and reported in ``warnings``. Nothing fuzzier is attempted.
- Context lines keep the file's own text; only ``-``/``+`` lines change.

The whole patch is applied in memory first; files are written only if every
hunk of every file applies. If a write fails midway, files already written
are restored.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from harness.tools.base import Tool, ToolContext, ToolFailure
from harness.tools.files import load_text_lines
from harness.tools.paths import relative_posix, resolve_in_repo

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_UNSUPPORTED = (
    ("rename from", "renames are not supported; delete the old file and create the new one"),
    ("rename to", "renames are not supported; delete the old file and create the new one"),
    ("copy from", "copies are not supported"),
    ("GIT binary patch", "binary patches are not supported"),
    ("Binary files", "binary patches are not supported"),
)


@dataclass
class _Hunk:
    old_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (op, text) op in " -+"
    old_no_newline: bool = False
    new_no_newline: bool = False

    @property
    def old_lines(self) -> list[str]:
        return [t for op, t in self.lines if op in " -"]

    @property
    def new_lines(self) -> list[str]:
        return [t for op, t in self.lines if op in " +"]


@dataclass
class _FilePatch:
    old_path: Optional[str]  # None for /dev/null (creation)
    new_path: Optional[str]  # None for /dev/null (deletion)
    hunks: list[_Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class PatchedFile:
    path: str
    action: str   # "modified", "created" or "deleted"
    hunks: int
    added: int
    removed: int
    before_sha256: Optional[str] = None   # content hash before this patch (None: file did not exist)
    after_sha256: Optional[str] = None    # content hash after this patch (None: file deleted)


@dataclass(frozen=True)
class PatchResult:
    files: tuple[PatchedFile, ...]
    warnings: tuple[str, ...] = ()

    @property
    def changed_files(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _header_path(raw: str) -> Optional[str]:
    path = raw.split("\t", 1)[0].strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_patch(text: str) -> list[_FilePatch]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    patches: list[_FilePatch] = []
    current: Optional[_FilePatch] = None
    hunk: Optional[_Hunk] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        for prefix, message in _UNSUPPORTED:
            if line.startswith(prefix):
                raise ToolFailure("patch_unsupported", message, {"line": i + 1})
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = _FilePatch(_header_path(line[4:]), _header_path(lines[i + 1][4:]))
            patches.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK_HEADER.match(line)
        if match:
            if current is None:
                raise ToolFailure("patch_invalid", f"line {i + 1}: hunk before any '---'/'+++' file header")
            hunk = _Hunk(old_start=int(match.group(1)))
            current.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None and (line[:1] in (" ", "-", "+") or line == ""):
            # A bare empty line is usually an empty context line whose leading
            # space was lost; "~" marks it so trailing ones can be dropped below.
            hunk.lines.append((line[:1] or "~", line[1:]))
        elif hunk is not None and line.startswith("\\"):
            if hunk.lines:
                op = hunk.lines[-1][0]
                if op in " -":
                    hunk.old_no_newline = True
                if op in " +":
                    hunk.new_no_newline = True
        elif hunk is not None and not line.startswith(("diff ", "index ", "new file mode", "deleted file mode",
                                                        "old mode", "new mode", "similarity index")):
            raise ToolFailure("patch_invalid", f"line {i + 1}: unexpected line inside a hunk: {line[:80]!r}")
        else:
            hunk = None  # git metadata between files, or preamble text
        i += 1

    for fp in patches:
        for h in fp.hunks:
            while h.lines and h.lines[-1][0] == "~":  # blank separator lines, not context
                h.lines.pop()
            h.lines = [(" " if op == "~" else op, t) for op, t in h.lines]
    if not patches:
        raise ToolFailure("patch_invalid", "no file changes found: expected '--- a/<path>' and '+++ b/<path>' headers")
    for fp in patches:
        if fp.old_path is None and fp.new_path is None:
            raise ToolFailure("patch_invalid", "a file header has /dev/null on both sides")
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            raise ToolFailure("patch_unsupported", f"renames are not supported ({fp.old_path} -> {fp.new_path})")
        if not fp.hunks:
            raise ToolFailure("patch_invalid", f"{fp.path}: no hunks")
        for n, h in enumerate(fp.hunks, start=1):
            if not any(op in "+-" for op, _ in h.lines):
                raise ToolFailure("patch_invalid", f"{fp.path}: hunk {n} changes nothing")
    return patches


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def _find(lines: list[str], block: list[str], lo: int, hint: int, loose: bool) -> Optional[int]:
    norm = (lambda s: s.rstrip()) if loose else (lambda s: s)
    want = [norm(b) for b in block]
    best: Optional[int] = None
    for pos in range(lo, len(lines) - len(block) + 1):
        if all(norm(lines[pos + k]) == want[k] for k in range(len(block))):
            if best is None or abs(pos - hint) < abs(best - hint):
                best = pos
    return best


def _mismatch(path: str, number: int, hunk: _Hunk, lines: list[str], lo: int, hint: int) -> ToolFailure:
    """Describe the closest near-match to help whoever wrote the patch."""
    block = hunk.old_lines
    best_pos, best_score = None, -1
    for pos in range(lo, max(lo, len(lines) - len(block)) + 1):
        score = sum(1 for k in range(len(block)) if pos + k < len(lines) and lines[pos + k] == block[k])
        if score > best_score or (score == best_score and best_pos is not None and abs(pos - hint) < abs(best_pos - hint)):
            best_pos, best_score = pos, score
    details: dict = {"file": path, "hunk": number, "expected": block[:20]}
    message = f"{path}: hunk {number} does not match the file (stated at line {hunk.old_start})"
    if best_pos is None or best_score <= 0:  # nothing matched: compare against the stated position
        best_pos, where = min(hint, max(len(lines) - 1, 0)), "at the stated position"
    else:
        where = f"closest match at line {best_pos + 1}"
    actual = lines[best_pos: best_pos + len(block)]
    first_diff = next((k for k in range(len(block)) if k >= len(actual) or actual[k] != block[k]), None)
    details.update(compared_at_line=best_pos + 1, actual=actual[:20])
    if first_diff is not None:
        got = actual[first_diff] if first_diff < len(actual) else "<end of file>"
        message += (f"; {where}, first difference at line {best_pos + first_diff + 1}: "
                    f"expected {block[first_diff]!r}, file has {got!r}")
    return ToolFailure("patch_mismatch", message, details)


def _apply_hunks(path: str, lines: list[str], ends_with_newline: bool, hunks: list[_Hunk],
                 warnings: list[str]) -> tuple[list[str], bool]:
    lines = list(lines)
    lo = 0
    offset = 0
    for number, hunk in enumerate(hunks, start=1):
        block = hunk.old_lines
        hint = max(hunk.old_start - 1 + offset, 0)
        if not block:  # pure insertion: "@@ -N,0" inserts after line N
            pos: Optional[int] = min(max(hunk.old_start + offset, lo), len(lines))
        else:
            pos = _find(lines, block, lo, hint, loose=False)
            if pos is None:
                pos = _find(lines, block, lo, hint, loose=True)
                if pos is None:
                    raise _mismatch(path, number, hunk, lines, lo, hint)
                warnings.append(f"{path}: hunk {number} matched only after ignoring trailing whitespace")
        replacement: list[str] = []
        cursor = pos
        for op, text in hunk.lines:
            if op == " ":
                replacement.append(lines[cursor])
                cursor += 1
            elif op == "-":
                cursor += 1
            else:
                replacement.append(text)
        touches_end = cursor == len(lines)
        lines[pos:cursor] = replacement
        lo = pos + len(replacement)
        offset += len(replacement) - (cursor - pos)
        if touches_end:
            ends_with_newline = not hunk.new_no_newline
    return lines, ends_with_newline


def _split(text_lines: list[str]) -> tuple[list[str], str, bool]:
    """(lines without endings, newline style, file ends with newline)."""
    newline = "\r\n" if text_lines and text_lines[0].endswith("\r\n") else "\n"
    ends = bool(text_lines) and text_lines[-1].endswith(("\n", "\r"))
    return [l.rstrip("\r\n") for l in text_lines], newline, ends


def _join(lines: list[str], newline: str, ends_with_newline: bool) -> str:
    if not lines:
        return ""
    return newline.join(lines) + (newline if ends_with_newline else "")


def apply_patch(ctx: ToolContext, patch: str) -> PatchResult:
    file_patches = parse_patch(patch)
    warnings: list[str] = []
    # path -> (resolved, original text or None if absent, new text or None if deleted)
    planned: dict[Path, tuple[Optional[str], Optional[str]]] = {}
    summary: dict[Path, list] = {}

    for fp in file_patches:
        target = resolve_in_repo(ctx.root, fp.path, for_write=True)
        rel = relative_posix(ctx.root, target)
        current = planned[target][1] if target in planned else _read_current(ctx, target)
        original = planned[target][0] if target in planned else current

        if fp.old_path is None:  # creation
            if current is not None:
                raise ToolFailure("patch_conflict", f"{rel}: cannot create, the file already exists")
            if any(op != "+" for h in fp.hunks for op, _ in h.lines):
                raise ToolFailure("patch_invalid", f"{rel}: a new-file patch may only add lines")
            new_lines = [t for h in fp.hunks for t in h.new_lines]
            new_text = _join(new_lines, "\n", not fp.hunks[-1].new_no_newline)
            action = "created"
        else:
            if current is None:
                raise ToolFailure("not_found", f"{rel}: file to patch does not exist")
            lines, newline, ends = _split(current.splitlines(keepends=True))
            new_lines, ends = _apply_hunks(rel, lines, ends, fp.hunks, warnings)
            if fp.new_path is None:  # deletion
                if new_lines:
                    raise ToolFailure("patch_mismatch",
                                      f"{rel}: deletion patch does not remove the whole file "
                                      f"({len(new_lines)} lines would remain)")
                new_text, action = None, "deleted"
            else:
                new_text, action = _join(new_lines, newline, ends), "modified"

        planned[target] = (original, new_text)
        added = sum(1 for h in fp.hunks for op, _ in h.lines if op == "+")
        removed = sum(1 for h in fp.hunks for op, _ in h.lines if op == "-")
        prev = summary.get(target)
        if prev:
            action = "created" if prev[1] == "created" else action
            summary[target] = [rel, action, prev[2] + len(fp.hunks), prev[3] + added, prev[4] + removed]
        else:
            summary[target] = [rel, action, len(fp.hunks), added, removed]

    _write_all(ctx, planned)
    files = tuple(PatchedFile(*summary[t], _sha256(planned[t][0]), _sha256(planned[t][1])) for t in planned)
    return PatchResult(files=files, warnings=tuple(warnings))


def _sha256(text: Optional[str]) -> Optional[str]:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_current(ctx: ToolContext, target: Path) -> Optional[str]:
    if not target.exists():
        return None
    _, lines, _ = load_text_lines(ctx, relative_posix(ctx.root, target))
    return "".join(lines)


def _write_all(ctx: ToolContext, planned: dict[Path, tuple[Optional[str], Optional[str]]]) -> None:
    done: list[tuple[Path, Optional[str]]] = []
    try:
        for target, (original, new_text) in planned.items():
            if new_text is None:
                target.unlink()
            else:
                _atomic_write(target, new_text)
            done.append((target, original))
    except OSError as exc:
        for target, original in reversed(done):
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, original)
            except OSError:
                pass
        raise ToolFailure("write_failed", f"could not write {relative_posix(ctx.root, target)}: "
                                          f"{exc.__class__.__name__}; earlier files restored") from None


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


TOOLS = (
    Tool(
        name="apply_patch",
        description="Apply a unified diff (as produced by 'git diff' or 'diff -u') to files in the "
                    "repository. Use '--- /dev/null' to create a file and '+++ /dev/null' to delete one. "
                    "Context lines must match the file exactly (trailing whitespace is tolerated). "
                    "All-or-nothing: if any hunk fails, no file is changed.",
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
            "additionalProperties": False,
        },
        handler=apply_patch,
        category="write",
    ),
)
