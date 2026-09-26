"""The repository boundary.

``resolve_in_repo`` is the only function that turns a caller-supplied path
into a filesystem path. Every tool uses it; none does its own checks.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from harness.tools.base import ToolFailure

# Directories never listed, searched or walked into.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__",
    ".venv", "venv",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".harness-runs",
})


def resolve_in_repo(root: Path, path: str, *, base: Optional[Path] = None,
                    for_write: bool = False) -> Path:
    """Resolve ``path`` and require the result to stay inside ``root``.

    - ``root`` must already be resolved (``ToolContext.create`` does this).
    - Relative paths are taken relative to ``base`` (default ``root``);
      absolute paths are accepted only if they land inside ``root``.
    - Symlinks are followed, so a link pointing outside the repository is
      rejected even when its own location is inside.
    - ``for_write`` additionally rejects anything inside ``.git``.
    """
    if not isinstance(path, str) or not path.strip():
        raise ToolFailure("invalid_path", "path must be a non-empty string")
    if "\x00" in path:
        raise ToolFailure("invalid_path", "path contains a NUL byte")
    if path.startswith("~"):
        raise ToolFailure("path_outside_repo", f"'{path}': home-directory paths are outside the repository")

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # e.g. symlink loop
        raise ToolFailure("invalid_path", f"'{path}' cannot be resolved: {exc.__class__.__name__}") from None

    if resolved != root and root not in resolved.parents:
        raise ToolFailure(
            "path_outside_repo",
            f"'{path}' resolves outside the repository root",
            {"path": path},
        )
    if for_write and ".git" in resolved.relative_to(root).parts:
        raise ToolFailure("path_not_writable", f"'{path}' is inside .git; the harness never writes there")
    return resolved


def relative_posix(root: Path, path: Path) -> str:
    """Repository-relative path with forward slashes; ``.`` for the root."""
    rel = path.relative_to(root)
    return PurePosixPath(*rel.parts).as_posix() if rel.parts else "."


def iter_repo_files(root: Path, start: Path, *, include_dirs: bool = False,
                    max_depth: Optional[int] = None) -> Iterator[tuple[Path, str]]:
    """Walk ``start`` deterministically (sorted), skipping ``IGNORED_DIRS``.

    Yields ``(path, kind)`` with kind ``file``, ``dir`` or ``symlink``.
    Symlinks are reported but never followed.
    """
    start_depth = len(start.relative_to(root).parts)
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts) - start_depth
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        entries = sorted([(d, True) for d in dirnames] + [(f, False) for f in filenames])
        for name, is_dir in entries:
            path = current / name
            if path.is_symlink():
                yield path, "symlink"
            elif is_dir:
                if include_dirs:
                    yield path, "dir"
            else:
                yield path, "file"
        if max_depth is not None and depth + 1 >= max_depth:
            dirnames[:] = []
        else:
            # os.walk does not descend into symlinked dirs (followlinks=False),
            # but they are still in dirnames; drop them to keep traversal explicit.
            dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
