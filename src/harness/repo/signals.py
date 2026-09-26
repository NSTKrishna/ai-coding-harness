"""Deterministic task-signal extraction (no model, no NLP).

From a task such as "Fix PaymentService refreshToken so expired tokens are
retried (see src/auth/token.py)" it extracts:

- explicit_paths: ``src/auth/token.py``
- identifiers:    ``PaymentService``, ``refreshToken`` (CamelCase, camelCase,
                  snake_case, SCREAMING_CASE, dotted names, `backticked`, calls())
- name_parts:     ``payment``, ``service``, ``refresh``, ``token`` (lowercased
                  pieces of identifiers, used for filename matching)
- keywords:       ``expired``, ``tokens``, ``retried`` (other words minus noise)
- phrases:        ``expired tokens`` (adjacent keywords)

Everything keeps first-appearance order and is deduplicated, so the same task
always gives the same signals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harness.repo.classify import LANGUAGES_BY_EXTENSION

NOISE_WORDS = frozenset("""
a about above after again against all also an and any are as at be because been before being
below between both but by can cannot could did do does doing done down during each else ensure
etc every few for from further get gets got had has have having he her here hers him his how i
if in into is it its itself just let lets like make makes may me might more most must my no nor
not now of off on once only or other our out over own please same she should so some such than
that the their them then there these they this those through to too under until up upon us use
used uses using very via was we were what when where which while who whom why will with would
you your yours
fix fixes fixed fixing bug bugs issue issues problem problems error errors add adds added adding
update updates updated change changes changed implement implements implemented support supports
handle handles handled correct correctly properly proper behavior behaviour work works working
new code file files function functions method methods class classes test tests testing value
values case cases need needs needed want wants instead currently current right wrong still
see look module modules thing things way
return returns returned returning raise raises raised throw throws thrown call calls called
true false none null nil undefined import imports self this
""".split())

_KNOWN_EXTENSIONS = tuple(sorted(set(LANGUAGES_BY_EXTENSION) | {".lock", ".env", ".cfg"}))
_PATH = re.compile(r"(?<![\w/.-])((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+/?|[\w-]+(?:\.[\w-]+)*\.[A-Za-z0-9]{1,5})(?::\d+)?")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_CAMEL = re.compile(r"\b(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*"
                    r"|[A-Z]{2,}[a-z][A-Za-z0-9]*)\b")  # PaymentService, refreshToken, HTTPClient
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_CALL = re.compile(r"\b([A-Za-z_]\w*)\(\)")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


@dataclass(frozen=True)
class TaskSignals:
    explicit_paths: tuple[str, ...]
    identifiers: tuple[str, ...]
    name_parts: tuple[str, ...]
    keywords: tuple[str, ...]
    phrases: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.explicit_paths or self.identifiers or self.keywords)


def split_identifier(identifier: str) -> list[str]:
    """``PaymentService`` / ``refresh_token`` / ``HTTPClient`` -> lowercase parts."""
    parts: list[str] = []
    for chunk in re.split(r"[_\-.\s]+", identifier):
        parts.extend(p.lower() for p in _CAMEL_PARTS.findall(chunk))
    return [p for p in parts if p]


def normalize_name(text: str) -> str:
    """Case/separator-insensitive form: ``ParseConfig`` == ``parse_config`` == ``parse-config``."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def keyword_stem(word: str) -> str:
    """Crude suffix stripping used for substring matching (``tokens``->``token``,
    ``retried``->``retr``, ``expired``->``expir``). Deterministic, not linguistic."""
    w = word.lower()
    for suffix in ("ies", "ied", "ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4 and not w.endswith(("ss", "us", "is")):
            return w[: -len(suffix)]
    return w


def _unique(items) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return tuple(seen)


def _looks_like_path(token: str) -> bool:
    if "/" in token:
        return any(ch.isalpha() for ch in token)
    return token.lower().endswith(_KNOWN_EXTENSIONS) and not token[0].isdigit()


def extract_task_signals(task: str) -> TaskSignals:
    text = _URL.sub(" ", task)

    paths = []
    for match in _PATH.finditer(text):
        token = match.group(1).rstrip("/.")
        if _looks_like_path(token):
            paths.append(token[2:] if token.startswith("./") else token)
    paths = list(_unique(paths))
    path_text = set(paths)

    identifiers: list[tuple[int, str]] = []
    for m in _BACKTICK.finditer(text):
        inner = m.group(1).strip().rstrip("()")
        if inner and inner not in path_text and re.fullmatch(r"[\w.:$-]+", inner):
            identifiers.append((m.start(), inner))
    for pattern in (_CAMEL, _SNAKE, _DOTTED):
        for m in pattern.finditer(text):
            token = m.group(0)
            if token in path_text or any(token in p for p in path_text):
                continue
            identifiers.append((m.start(), token))
    for m in _CALL.finditer(text):
        identifiers.append((m.start(), m.group(1)))
    for m in re.finditer(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text):
        identifiers.append((m.start(), m.group(0)))
    ordered_ids = list(_unique(t for _, t in sorted(identifiers, key=lambda x: (x[0], -len(x[1])))))
    # Dotted names contribute their last component as an identifier too.
    for ident in list(ordered_ids):
        if "." in ident:
            ordered_ids.append(ident.rsplit(".", 1)[-1])
    ids = _unique(ordered_ids)

    parts = _unique(p for ident in ids for p in split_identifier(ident)
                    if len(p) >= 3 and p not in NOISE_WORDS)

    covered = {normalize_name(i) for i in ids} | {p for p in parts}
    for path in paths:
        covered.update(normalize_name(seg) for seg in re.split(r"[/.]", path))
    words = [w for w in _WORD.findall(_strip_code(text, ids, paths))]
    keywords = _unique(w.lower() for w in words
                       if len(w) >= 3 and w.lower() not in NOISE_WORDS and w.lower() not in covered)

    phrases = []
    for clause in re.split(r"[.,;:!?()\[\]{}\n]+", _strip_code(text, ids, paths)):
        tokens = [w.lower() for w in _WORD.findall(clause)]
        for a, b in zip(tokens, tokens[1:]):
            if a in keywords and b in keywords:
                phrases.append(f"{a} {b}")

    return TaskSignals(explicit_paths=tuple(paths), identifiers=ids, name_parts=parts,
                       keywords=keywords, phrases=_unique(phrases))


def _strip_code(text: str, identifiers, paths) -> str:
    """Remove identifiers and paths so their pieces are not counted as plain words."""
    for token in sorted(set(identifiers) | set(paths), key=len, reverse=True):
        text = text.replace(token, " ")
    return text
