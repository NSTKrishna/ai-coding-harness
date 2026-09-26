"""Bounded, counted reads for repository intelligence.

Every content read made by the analyzer and discovery goes through
``RepoReader`` so the discovery metrics (files read, bytes read) are real.
Paths go through the M2 boundary (``resolve_in_repo`` via ``load_text_lines``).
"""

from __future__ import annotations

from typing import Optional

from harness.tools.base import ToolContext, ToolFailure
from harness.tools.files import BINARY_SNIFF_BYTES, load_text_lines
from harness.tools.paths import resolve_in_repo


class RepoReader:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.files_read: set[str] = set()
        self.bytes_read = 0
        self._lines: dict[str, Optional[list[str]]] = {}

    def head(self, rel: str, max_bytes: int = 16_384) -> Optional[str]:
        """The first ``max_bytes`` of a text file, or ``None`` (missing, binary, outside)."""
        if rel in self._lines:  # already fully read
            lines = self._lines[rel]
            return None if lines is None else "".join(lines)[:max_bytes]
        try:
            path = resolve_in_repo(self.ctx.root, rel)
            with open(path, "rb") as handle:
                data = handle.read(max_bytes)
        except (ToolFailure, OSError):
            return None
        self._count(rel, len(data))
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            return None
        return data.decode("utf-8", "replace")

    def lines(self, rel: str) -> Optional[list[str]]:
        """All lines (with endings) of a UTF-8 text file within ``max_file_bytes``; cached."""
        if rel not in self._lines:
            try:
                _, lines, size = load_text_lines(self.ctx, rel)
                self._count(rel, size)
            except ToolFailure:
                lines = None
            self._lines[rel] = lines
        return self._lines[rel]

    def _count(self, rel: str, size: int) -> None:
        self.files_read.add(rel)
        self.bytes_read += size
