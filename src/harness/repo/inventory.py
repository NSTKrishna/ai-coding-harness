"""Repository inventory: which files count as part of the repository.

Git repositories use git's own view: tracked files plus untracked files that
are not ignored (``git ls-files --cached`` + ``--others --exclude-standard``).
Other directories use a deterministic filesystem walk.

In both modes the inventory also drops:

- anything under ``tools.paths.IGNORED_DIRS`` (``.git``, virtualenvs,
  ``node_modules``, caches), even if git would list it;
- build-output directories (``BUILD_OUTPUT_DIRS``), unless git tracks the file;
- symlinks (they may point outside the repository) and missing files.

Nothing is read; only ``stat`` is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harness.tools.base import ToolContext, ToolFailure
from harness.tools.git import git_list_files, git_work_tree_prefix
from harness.tools.paths import IGNORED_DIRS, iter_repo_files, relative_posix
from harness.repo.classify import category_of, language_of

BUILD_OUTPUT_DIRS = frozenset({
    "build", "dist", "target", "out", "coverage", "htmlcov", ".next", ".nuxt", ".eggs",
    ".gradle", ".parcel-cache", ".turbo",
})
DEFAULT_MAX_FILES = 50_000


@dataclass(frozen=True)
class FileRecord:
    path: str                 # repository-relative, forward slashes
    size_bytes: int
    language: Optional[str]
    category: str             # see classify.CATEGORIES
    tracked: Optional[bool]   # None outside git


@dataclass(frozen=True)
class Inventory:
    root: Path
    source: str               # "git" or "filesystem"
    files: tuple[FileRecord, ...]
    truncated: bool           # more than max_files existed
    warnings: tuple[str, ...] = ()

    @property
    def is_git(self) -> bool:
        return self.source == "git"

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    def get(self, path: str) -> Optional[FileRecord]:
        return self._index().get(path)

    def _index(self) -> dict[str, FileRecord]:
        index = self.__dict__.get("_by_path")
        if index is None:
            index = {f.path: f for f in self.files}
            object.__setattr__(self, "_by_path", index)
        return index


def _excluded(rel: str, tracked: Optional[bool]) -> bool:
    parts = rel.split("/")
    if IGNORED_DIRS.intersection(parts[:-1]) or parts[-1] in IGNORED_DIRS:
        return True
    return not tracked and bool(BUILD_OUTPUT_DIRS.intersection(parts[:-1]))


def build_inventory(ctx: ToolContext, *, max_files: int = DEFAULT_MAX_FILES) -> Inventory:
    warnings: list[str] = []
    entries: Optional[list[tuple[str, Optional[bool]]]] = None
    source = "filesystem"
    if git_work_tree_prefix(ctx) is not None:
        try:
            tracked = git_list_files(ctx, tracked=True)
            untracked = git_list_files(ctx, tracked=False)
            entries = [(p, True) for p in tracked] + [(p, False) for p in untracked]
            source = "git"
        except ToolFailure as exc:
            warnings.append(f"git listing failed ({exc.code}); used a filesystem walk instead")
    if entries is None:
        entries = [(relative_posix(ctx.root, p), None)
                   for p, kind in iter_repo_files(ctx.root, ctx.root) if kind == "file"]

    records: list[FileRecord] = []
    truncated = False
    for rel, tracked in sorted(set(entries)):
        if _excluded(rel, tracked):
            continue
        path = ctx.root / rel
        if path.is_symlink() or not path.is_file():
            continue  # symlink, submodule directory, or tracked-but-deleted file
        if len(records) >= max_files:
            truncated = True
            break
        language = language_of(rel)
        records.append(FileRecord(rel, path.stat().st_size, language, category_of(rel, language), tracked))
    if truncated:
        warnings.append(f"inventory stopped at {max_files} files")
    return Inventory(root=ctx.root, source=source, files=tuple(records), truncated=truncated,
                     warnings=tuple(warnings))
