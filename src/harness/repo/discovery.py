"""Deterministic candidate discovery and ranking for a task. No model calls.

Stages, cheapest first:

1. explicit paths named in the task                         (inventory only)
2. file / directory names matching identifiers and keywords (inventory only)
3. identifier content search (+ camel/snake variants)       (search_text over the inventory)
4. keyword and phrase content search — skipped when identifier search
   already found a definition or at least ``KEYWORD_SEARCH_THRESHOLD`` files;
   a keyword matching too many files (``COMMON_KEYWORD_*``) is ignored
5. multi-signal bonus
6. source <-> test pairing boost
7. one-hop import expansion from the top files              (bounded reads)
8. documentation files weighted down

Score weights (points; a file's score is the sum of its reasons):

    explicit path in task ............................ 100
    file name mentioned in task (no directory) ....... 80
    inside a directory named in the task ............. 30
    file name equals an identifier ................... 40
    defines an identifier ............................ 30
    contains an identifier ........................... 20   (+2 per extra hit, max +6)
    file name matches an identifier part / keyword ... 12 / 10  (prefix: 8 / 6)
    directory name matches a part / keyword .......... 6
    contains a task phrase ........................... 8
    contains a keyword ............................... 4    (+1 per extra hit, max +3)
    each distinct task signal beyond the first ....... 5    (max 15)
    test <-> source pair of a top file ............... half the partner's score, max 20
    imported by a top file ........................... 8 if the import mentions a signal, else 3
    documentation files .............................. score halved

Every point comes with a human-readable reason.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from harness.config import ContextLimits
from harness.context.working_set import CandidateSummary, Evidence, WorkingSet, build_working_set, make_snippet
from harness.repo.inventory import FileRecord, Inventory
from harness.repo.profile import RepoProfile, RepositoryAnalyzer
from harness.repo.reader import RepoReader
from harness.repo.signals import TaskSignals, extract_task_signals, keyword_stem, normalize_name, split_identifier
from harness.tools.base import ToolContext, ToolFailure
from harness.tools.search import search

W_EXPLICIT_PATH = 100
W_MENTIONED_FILENAME = 80
W_EXPLICIT_DIR_MEMBER = 30
W_FILENAME_IDENTIFIER = 40
W_DEFINES = 30
W_CONTAINS = 20
W_EXTRA_HIT = 2
W_EXTRA_HIT_MAX = 6
W_FILENAME_PART = 12
W_FILENAME_PART_PREFIX = 8
W_FILENAME_KEYWORD = 10
W_FILENAME_KEYWORD_PREFIX = 6
W_DIR_MATCH = 6
W_PHRASE = 8
W_KEYWORD = 4
W_KEYWORD_EXTRA_MAX = 3
W_MULTI_SIGNAL = 5
W_MULTI_SIGNAL_MAX = 15
W_PAIR_MAX = 20
W_IMPORT_SIGNAL = 8
W_IMPORT = 3

SEARCHABLE_CATEGORIES = frozenset({"source", "test", "configuration", "documentation", "manifest", "ci"})
MAX_IDENTIFIER_SEARCHES = 8
MAX_KEYWORD_SEARCHES = 6
MAX_PHRASE_SEARCHES = 3
MAX_MATCHES_PER_SEARCH = 200
KEYWORD_SEARCH_THRESHOLD = 3     # identifier-matched files needed to skip keyword search
COMMON_KEYWORD_MIN_FILES = 20    # a keyword in at least this many files...
COMMON_KEYWORD_FRACTION = 0.3    # ...and this share of searchable files carries no signal
MAX_EXPLICIT_DIR_FILES = 20
PAIRING_TOP = 10
PAIRING_MIN_SCORE = 20
IMPORT_EXPANSION_TOP = 5
IMPORT_SCAN_LINES = 300
MAX_IMPORTS_PER_FILE = 10
MAX_CANDIDATES_KEPT = 50
MAX_HITS_PER_CANDIDATE = 5

_DEFINITION_TEMPLATES = (
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:def|class|function|fn|interface|type|struct|enum|trait|const|let|var|val)\s+{v}\b",
    r"^\s*func\s+(?:\([^)]*\)\s*)?{v}\s*[\[(]",
    r"^\s*(?:(?:public|private|protected|static|async|readonly|final|override|abstract)\s+)*"
    r"(?:[\w<>\[\],.?]+\s+)?{v}\s*\([^;]*\)\s*(?::[^={;]+)?\{\s*$",
    r"^{v}\s*(?::[^=]+)?=[^=]",
)


@dataclass(frozen=True)
class SignalHit:
    line: int
    text: str
    signal: str
    kind: str      # "definition", "reference", "phrase" or "keyword"


@dataclass(frozen=True)
class CandidateFile:
    path: str
    score: int
    reasons: tuple[str, ...]
    matched_signals: tuple[str, ...]
    evidence: tuple[SignalHit, ...]   # strongest content hits, at most MAX_HITS_PER_CANDIDATE
    category: str


@dataclass
class DiscoveryMetrics:
    inventory_files: int = 0
    searchable_files: int = 0
    content_searches: int = 0
    files_matched: int = 0            # distinct files with at least one content match
    analysis_files_read: int = 0      # manifests/config/test heads read by the analyzer
    discovery_files_read: int = 0     # files read for evidence and import expansion
    bytes_read: int = 0               # total, analysis + discovery
    candidates: int = 0
    selected_files: int = 0
    evidence_items: int = 0
    evidence_omitted: int = 0
    working_set_chars: int = 0
    keyword_search: str = "not needed"

    def summary(self) -> str:
        return (f"{self.inventory_files} files in inventory, {self.searchable_files} searchable; "
                f"{self.content_searches} content searches matched {self.files_matched} files; "
                f"read {self.analysis_files_read} config + {self.discovery_files_read} candidate files "
                f"({self.bytes_read:,} bytes); {self.candidates} candidates, {self.selected_files} selected, "
                f"{self.evidence_items} evidence items ({self.evidence_omitted} omitted), "
                f"{self.working_set_chars:,} chars; keyword search: {self.keyword_search}")


@dataclass(frozen=True)
class DiscoveryResult:
    repo_profile: RepoProfile
    task_signals: TaskSignals
    candidates: tuple[CandidateFile, ...]
    working_set: WorkingSet
    metrics: DiscoveryMetrics
    warnings: tuple[str, ...]

    @property
    def test_commands(self):
        return self.repo_profile.test_commands

    @property
    def build_commands(self):
        return self.repo_profile.build_commands


@dataclass
class _Acc:
    record: FileRecord
    points: list[tuple[int, str, str]] = field(default_factory=list)   # (points, reason, signal)
    hits: list[SignalHit] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(p for p, _, _ in self.points)

    @property
    def signals(self) -> list[str]:
        return list(dict.fromkeys(s for _, _, s in self.points if s))

    def add(self, points: int, reason: str, signal: str = "") -> None:
        self.points.append((points, reason, signal))


# --------------------------------------------------------------------------
# Name helpers
# --------------------------------------------------------------------------

def base_key(path: str) -> str:
    """Normalized file stem with test markers removed: tests/test_service.py -> 'service',
    token.test.ts -> 'token', parser_test.go -> 'parser', FooTest.java -> 'foo'."""
    name = PurePosixPath(path).name
    stem = name.split(".", 1)[0] if name.count(".") >= 1 else name
    for marker in (".test.", ".spec."):
        if marker in name:
            stem = name.split(marker, 1)[0]
    stem = re.sub(r"^test_", "", stem)
    stem = re.sub(r"(_test|_spec|Tests?|Spec|IT)$", "", stem) or stem
    return normalize_name(stem)


def _family(record: FileRecord) -> str:
    lang = record.language or ""
    if lang.startswith(("TypeScript", "JavaScript")):
        return "js"
    if lang in ("Java", "Kotlin", "Scala"):
        return "jvm"
    return lang


def _definition_regex(variant: str) -> re.Pattern:
    v = re.escape(variant)
    return re.compile("|".join(t.replace("{v}", v) for t in _DEFINITION_TEMPLATES))


def identifier_variants(identifier: str) -> list[str]:
    parts = split_identifier(identifier)
    variants = [identifier]
    if len(parts) >= 2 and "." not in identifier and not identifier.isupper():
        variants += ["_".join(parts), parts[0] + "".join(p.title() for p in parts[1:]),
                     "".join(p.title() for p in parts)]
    return list(dict.fromkeys(variants))


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class Discoverer:
    def __init__(self, ctx: ToolContext, inventory: Inventory, reader: RepoReader,
                 signals: TaskSignals, metrics: DiscoveryMetrics) -> None:
        self.ctx = ctx
        self.inventory = inventory
        self.reader = reader
        self.signals = signals
        self.metrics = metrics
        self.warnings: list[str] = []
        self.acc: dict[str, _Acc] = {}
        self.scope = [f.path for f in inventory.files
                      if f.category in SEARCHABLE_CATEGORIES and f.size_bytes <= ctx.limits.max_file_bytes]
        self._matched: set[str] = set()
        self._by_name: dict[str, list[str]] = {}
        for rel in inventory.paths:
            self._by_name.setdefault(PurePosixPath(rel).name, []).append(rel)
        metrics.inventory_files = len(inventory.files)
        metrics.searchable_files = len(self.scope)

    def _with_suffix(self, suffix: str) -> list[str]:
        """Inventory paths equal to ``suffix`` or ending in ``/suffix`` (via the name index)."""
        return [p for p in self._by_name.get(PurePosixPath(suffix).name, ())
                if p == suffix or p.endswith("/" + suffix)]

    def _get(self, path: str) -> _Acc:
        if path not in self.acc:
            self.acc[path] = _Acc(self.inventory.get(path))
        return self.acc[path]

    def run(self) -> list[CandidateFile]:
        self._explicit_paths()
        self._names()
        id_files, id_definitions = self._identifier_search()
        if id_definitions:
            if self.signals.keywords:
                self.metrics.keyword_search = f"skipped (identifier definitions found in {id_definitions} files)"
        elif id_files >= KEYWORD_SEARCH_THRESHOLD:
            if self.signals.keywords:
                self.metrics.keyword_search = f"skipped ({id_files} files matched identifiers)"
        elif self.signals.keywords or self.signals.phrases:
            self._keyword_search()
            self.metrics.keyword_search = "run"
        self._multi_signal_bonus()
        self._pairing()
        self._imports()
        self._docs_weight()
        self.metrics.files_matched = len(self._matched)

        ranked = sorted((a for a in self.acc.values() if a.score > 0),
                        key=lambda a: (-a.score, a.record.path))[:MAX_CANDIDATES_KEPT]
        order = {"definition": 0, "reference": 1, "phrase": 2, "keyword": 3}
        return [CandidateFile(
            path=a.record.path, score=a.score,
            reasons=tuple(r for _, r, _ in sorted(a.points, key=lambda p: -p[0])),
            matched_signals=tuple(a.signals),
            evidence=tuple(sorted(a.hits, key=lambda h: (order[h.kind], h.line))[:MAX_HITS_PER_CANDIDATE]),
            category=a.record.category,
        ) for a in ranked]

    # 1 ---------------------------------------------------------------------
    def _explicit_paths(self) -> None:
        paths = self.inventory.paths
        for raw in self.signals.explicit_paths:
            wanted = raw.lstrip("/") if not raw.startswith(str(self.ctx.root)) else \
                raw[len(str(self.ctx.root)):].lstrip("/")
            if self.inventory.get(wanted):
                self._get(wanted).add(W_EXPLICIT_PATH, f"explicit path in task: {raw}", raw)
                continue
            suffix = [p for p in self._with_suffix(wanted) if p != wanted]
            if suffix and "/" in wanted:
                for p in suffix:
                    self._get(p).add(W_EXPLICIT_PATH, f"matches path '{raw}' from task", raw)
                continue
            members = [p for p in paths if p.startswith(wanted.rstrip("/") + "/")]
            if members:
                for p in members[:MAX_EXPLICIT_DIR_FILES]:
                    self._get(p).add(W_EXPLICIT_DIR_MEMBER, f"inside directory '{raw}' named in task", raw)
                continue
            named = list(self._by_name.get(wanted, ()))
            if named:
                for p in named:
                    self._get(p).add(W_MENTIONED_FILENAME, f"file name '{raw}' mentioned in task", raw)
                continue
            self.warnings.append(f"task mentions '{raw}', which is not in the repository inventory")

    # 2 ---------------------------------------------------------------------
    def _names(self) -> None:
        ids = {normalize_name(i): i for i in self.signals.identifiers if len(normalize_name(i)) >= 3}
        parts = [p for p in self.signals.name_parts if len(p) >= 3]
        stems = {keyword_stem(k): k for k in self.signals.keywords if len(keyword_stem(k)) >= 3}
        for record in self.inventory.files:
            if record.category not in SEARCHABLE_CATEGORIES:
                continue
            key = base_key(record.path)
            key_parts = set(split_identifier(PurePosixPath(record.path).name.split(".", 1)[0]))
            best: Optional[tuple[int, str, str]] = None

            def offer(points, reason, signal):
                nonlocal best
                if best is None or points > best[0]:
                    best = (points, reason, signal)

            if key in ids:
                offer(W_FILENAME_IDENTIFIER, f"file name matches identifier {ids[key]}", ids[key])
            for part in parts:
                if part == key or part in key_parts:
                    offer(W_FILENAME_PART, f"file name matches '{part}' (from identifier)", part)
                elif len(part) >= 4 and key.startswith(part):
                    offer(W_FILENAME_PART_PREFIX, f"file name starts with '{part}' (from identifier)", part)
            for stem, word in stems.items():
                if stem == key or stem in key_parts:
                    offer(W_FILENAME_KEYWORD, f"file name matches keyword '{word}'", word)
                elif len(stem) >= 4 and key.startswith(stem):
                    offer(W_FILENAME_KEYWORD_PREFIX, f"file name starts with keyword '{word}'", word)
            if best:
                self._get(record.path).add(*best)

            dirs = [normalize_name(d) for d in PurePosixPath(record.path).parts[:-1]]
            dir_signals = [(p, p) for p in parts] + [(s, w) for s, w in stems.items()]
            for d in dirs:
                hit = next((w for s, w in dir_signals if s == d or (len(s) >= 4 and d.startswith(s))), None)
                if hit:
                    self._get(record.path).add(W_DIR_MATCH, f"directory '{d}' matches '{hit}'", hit)
                    break

    # 3 ---------------------------------------------------------------------
    def _search(self, query: str, *, regex: bool, case_sensitive: bool):
        self.metrics.content_searches += 1
        try:
            result = search(self.ctx, query, regex=regex, case_sensitive=case_sensitive,
                            max_results=MAX_MATCHES_PER_SEARCH, allowed_paths=self.scope)
        except ToolFailure as exc:
            self.warnings.append(f"search for {query!r} failed: {exc.message}")
            return []
        if result.truncated:
            self.warnings.append(f"search for {query!r} stopped at {MAX_MATCHES_PER_SEARCH} matches")
        self._matched.update(m.path for m in result.matches)
        return result.matches

    def _identifier_search(self) -> tuple[int, int]:
        """Returns (files matched, files with a definition)."""
        files: set[str] = set()
        defining: set[str] = set()
        for ident in self.signals.identifiers[:MAX_IDENTIFIER_SEARCHES]:
            variants = identifier_variants(ident)
            pattern = r"\b(?:" + "|".join(re.escape(v) for v in variants) + r")\b"
            definitions = [(_definition_regex(v), v) for v in variants]
            per_file: dict[str, list[SignalHit]] = {}
            for m in self._search(pattern, regex=True, case_sensitive=True):
                kind = "definition" if any(rx.search(m.text) for rx, _ in definitions) else "reference"
                per_file.setdefault(m.path, []).append(SignalHit(m.line, m.text.strip(), ident, kind))
            for path, hits in per_file.items():
                acc = self._get(path)
                acc.hits.extend(hits)
                files.add(path)
                defs = [h for h in hits if h.kind == "definition"]
                extra = min(W_EXTRA_HIT * (len(hits) - 1), W_EXTRA_HIT_MAX)
                if defs:
                    defining.add(path)
                    acc.add(W_DEFINES + extra, f"defines {ident} (line {defs[0].line})", ident)
                elif acc.record.category == "test":
                    acc.add(W_CONTAINS + extra, f"test references {ident} ({len(hits)} match{'es' * (len(hits) > 1)})", ident)
                else:
                    acc.add(W_CONTAINS + extra, f"contains {ident} ({len(hits)} match{'es' * (len(hits) > 1)})", ident)
        return len(files), len(defining)

    # 4 ---------------------------------------------------------------------
    def _keyword_search(self) -> None:
        for word in self.signals.keywords[:MAX_KEYWORD_SEARCHES]:
            stem = keyword_stem(word)
            if len(stem) < 3:
                continue
            per_file: dict[str, list] = {}
            for m in self._search(stem, regex=False, case_sensitive=False):
                per_file.setdefault(m.path, []).append(m)
            if len(per_file) >= max(COMMON_KEYWORD_MIN_FILES, COMMON_KEYWORD_FRACTION * len(self.scope)):
                self.warnings.append(f"keyword '{word}' appears in {len(per_file)}+ files; ignored as too common")
                continue
            for path, matches in per_file.items():
                acc = self._get(path)
                acc.hits.extend(SignalHit(m.line, m.text.strip(), word, "keyword") for m in matches[:3])
                extra = min(len(matches) - 1, W_KEYWORD_EXTRA_MAX)
                acc.add(W_KEYWORD + extra, f"mentions '{word}' ({len(matches)}x)", word)
        for phrase in self.signals.phrases[:MAX_PHRASE_SEARCHES]:
            a, b = (keyword_stem(w) for w in phrase.split(" ", 1))
            pattern = re.escape(a) + r"\w*[\s_-]*" + re.escape(b)
            seen: set[str] = set()
            for m in self._search(pattern, regex=True, case_sensitive=False):
                acc = self._get(m.path)
                acc.hits.append(SignalHit(m.line, m.text.strip(), phrase, "phrase"))
                if m.path not in seen:
                    seen.add(m.path)
                    acc.add(W_PHRASE, f"contains phrase '{phrase}' (line {m.line})", phrase)

    # 5 ---------------------------------------------------------------------
    def _multi_signal_bonus(self) -> None:
        for acc in self.acc.values():
            n = len(acc.signals)
            if n >= 2:
                acc.add(min(W_MULTI_SIGNAL * (n - 1), W_MULTI_SIGNAL_MAX), f"matches {n} task signals")

    # 6 ---------------------------------------------------------------------
    def _pairing(self) -> None:
        by_key: dict[tuple[str, str], list[FileRecord]] = {}
        for record in self.inventory.files:
            if record.category in ("source", "test"):
                by_key.setdefault((base_key(record.path), _family(record)), []).append(record)
        top = sorted(self.acc.values(), key=lambda a: (-a.score, a.record.path))[:PAIRING_TOP]
        boosts: dict[str, tuple[int, str]] = {}
        for acc in top:
            if acc.score < PAIRING_MIN_SCORE or acc.record.category not in ("source", "test"):
                continue
            want = "source" if acc.record.category == "test" else "test"
            partners = [r for r in by_key.get((base_key(acc.record.path), _family(acc.record)), [])
                        if r.category == want]
            if not partners:
                continue
            partner = sorted(partners, key=lambda r: (-_affinity(acc.record.path, r.path), r.path))[0]
            points = min(W_PAIR_MAX, acc.score // 2)
            label = "test for" if want == "test" else "source under test by"
            if points > boosts.get(partner.path, (0, ""))[0]:
                boosts[partner.path] = (points, f"{label} top candidate {acc.record.path}")
        for path, (points, reason) in sorted(boosts.items()):
            self._get(path).add(points, reason)

    # 7 ---------------------------------------------------------------------
    def _imports(self) -> None:
        top = sorted(self.acc.values(), key=lambda a: (-a.score, a.record.path))[:IMPORT_EXPANSION_TOP]
        terms = [t.lower() for t in (*self.signals.identifiers, *self.signals.name_parts)] + \
                [keyword_stem(k) for k in self.signals.keywords]
        go_module = self._go_module()
        boosts: dict[str, tuple[int, str]] = {}
        for acc in top:
            if acc.record.category not in ("source", "test"):
                continue
            lines = self.reader.lines(acc.record.path)
            if lines is None:
                continue
            count = 0
            for line in lines[:IMPORT_SCAN_LINES]:
                targets = self._resolve_import(acc.record, line, go_module)
                if not targets:
                    continue
                count += 1
                mentions = next((t for t in terms if t and t in line.lower()), None)
                points = W_IMPORT_SIGNAL if mentions else W_IMPORT
                reason = f"imported by top candidate {acc.record.path}" + (f" (import mentions '{mentions}')" if mentions else "")
                for target in targets:
                    if target != acc.record.path and points > boosts.get(target, (0, ""))[0]:
                        boosts[target] = (points, reason)
                if count >= MAX_IMPORTS_PER_FILE:
                    break
        for path, (points, reason) in sorted(boosts.items()):
            self._get(path).add(points, reason)

    def _go_module(self) -> Optional[str]:
        if not self.inventory.get("go.mod"):
            return None
        head = self.reader.head("go.mod", 4096) or ""
        m = re.search(r"^module\s+(\S+)", head, re.MULTILINE)
        return m.group(1) if m else None

    def _resolve_import(self, record: FileRecord, line: str, go_module: Optional[str]) -> list[str]:
        lang = record.language or ""
        here = posixpath.dirname(record.path)
        if lang == "Python":
            m = re.match(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+([\w, ]+)", line) or \
                re.match(r"^\s*import\s+()([\w.]+)()", line)
            if not m:
                return []
            dots, module, names = m.group(1), m.group(2), m.group(3) or ""
            bases = [module] + [f"{module}.{n.strip()}" if module else n.strip()
                                for n in names.split(",") if n.strip()][:3]
            found: list[str] = []
            for dotted in bases:
                rel = dotted.replace(".", "/")
                if dots:
                    anchor = here
                    for _ in range(len(dots) - 1):
                        anchor = posixpath.dirname(anchor)
                    options = [posixpath.join(anchor, rel) if rel else anchor]
                else:
                    options = [rel]
                for opt in options:
                    for cand in (f"{opt}.py", f"{opt}/__init__.py"):
                        found += self._with_suffix(cand)[:2]
            return list(dict.fromkeys(found))
        if lang.startswith(("TypeScript", "JavaScript")):
            m = re.search(r"""(?:from\s+|require\(\s*|import\(\s*|^\s*import\s+)['"](\.{1,2}/[^'"]+)['"]""", line)
            if not m:
                return []
            base = posixpath.normpath(posixpath.join(here, m.group(1)))
            for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js"):
                if self.inventory.get(base + ext):
                    return [base + ext]
            return []
        if lang == "Go" and go_module:
            m = re.match(r'^\s*(?:import\s+)?(?:\w+\s+)?"([^"]+)"', line)
            if not m or not m.group(1).startswith(go_module + "/"):
                return []
            directory = m.group(1)[len(go_module) + 1:]
            return [p for p in self.inventory.paths
                    if posixpath.dirname(p) == directory and p.endswith(".go") and not p.endswith("_test.go")][:3]
        if lang in ("Java", "Kotlin"):
            m = re.match(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;?", line)
            if not m:
                return []
            rel = m.group(1).replace(".", "/")
            return (self._with_suffix(rel + ".java") + self._with_suffix(rel + ".kt"))[:2]
        return []

    # 8 ---------------------------------------------------------------------
    def _docs_weight(self) -> None:
        for acc in self.acc.values():
            if acc.record.category == "documentation" and acc.score > 0:
                acc.add(-(acc.score - acc.score // 2), "documentation file (score halved)")

    # evidence -----------------------------------------------------------------
    def evidence_pool(self, candidates: list[CandidateFile], limits: ContextLimits) -> list[Evidence]:
        pool: list[Evidence] = []
        weights = {"definition": 1.0, "reference": 0.8, "phrase": 0.6, "keyword": 0.5}
        n = limits.max_snippet_lines
        before = n // 3
        for cand in candidates[: limits.max_active_files]:
            lines = self.reader.lines(cand.path)
            if lines is None:
                continue
            acc = self.acc[cand.path]
            hits = sorted(acc.hits, key=lambda h: (-weights[h.kind], h.line))
            windows: list[list] = []   # [start, end, hit]
            for hit in hits:
                start = max(1, hit.line - before)
                end = min(len(lines), start + n - 1)
                if any(w[0] <= hit.line <= w[1] for w in windows):
                    continue
                windows.append([start, end, hit])
            if not windows:
                end = min(len(lines), n)
                if end:
                    pool.append(Evidence(cand.path, 1, end, make_snippet(lines, 1, end),
                                         f"start of file; {cand.reasons[0]}", "", 0.4 * cand.score))
                continue
            for start, end, hit in windows:
                label = {"definition": "defines", "reference": "references",
                         "phrase": "phrase", "keyword": "keyword"}[hit.kind]
                pool.append(Evidence(cand.path, start, end, make_snippet(lines, start, end),
                                     f"{label} {hit.signal} at line {hit.line}", hit.signal,
                                     weights[hit.kind] * cand.score))
        return pool


def _affinity(a: str, b: str) -> int:
    """Shared directory names, ignoring test/source container names."""
    ignore = {"src", "lib", "tests", "test", "__tests__", "spec", "internal", "pkg", "main", "java"}
    da = [d for d in PurePosixPath(a).parts[:-1] if d not in ignore]
    db = [d for d in PurePosixPath(b).parts[:-1] if d not in ignore]
    same_dir = 5 if posixpath.dirname(a) == posixpath.dirname(b) else 0
    return same_dir + len(set(da) & set(db))


def discover_for_task(repo, task: str, *, limits: Optional[ContextLimits] = None,
                      ctx: Optional[ToolContext] = None) -> DiscoveryResult:
    """Profile the repository and rank files for ``task``. No model, no API key, no writes."""
    limits = limits or ContextLimits()
    ctx = ctx or ToolContext.create(repo)
    reader = RepoReader(ctx)
    analyzer = RepositoryAnalyzer(ctx, reader)
    profile = analyzer.analyze()
    metrics = DiscoveryMetrics(analysis_files_read=len(reader.files_read))
    analysis_read = set(reader.files_read)

    signals = extract_task_signals(task)
    discoverer = Discoverer(ctx, analyzer.inventory, reader, signals, metrics)
    warnings = list(profile.warnings)
    if signals.empty:
        warnings.append("no usable signals in the task (no paths, identifiers or keywords)")
    candidates = discoverer.run()
    pool = discoverer.evidence_pool(candidates, limits)
    working_set = build_working_set(
        profile.summary(),
        [CandidateSummary(c.path, c.score, c.reasons) for c in candidates],
        pool, limits)

    metrics.discovery_files_read = len(reader.files_read - analysis_read)
    metrics.bytes_read = reader.bytes_read
    metrics.candidates = len(candidates)
    metrics.selected_files = len(working_set.selected_files)
    metrics.evidence_items = len(working_set.evidence)
    metrics.evidence_omitted = working_set.evidence_omitted
    metrics.working_set_chars = working_set.estimated_chars
    return DiscoveryResult(profile, signals, tuple(candidates), working_set, metrics,
                           tuple(warnings + discoverer.warnings))
