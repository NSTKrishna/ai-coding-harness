"""Planner: one model call that turns the task plus M3's bounded context into a TaskPlan.

The planner never sees the repository directly. Its input is the task (capped),
the task signals, ``WorkingSet.render()`` (already bounded by ``ContextLimits``)
and the discovered verification commands. Its output must be one strict JSON
object (see ``PLAN_SCHEMA``). Anything malformed is rejected, never repaired or
filled in.

``verification_candidates`` may only name commands the harness discovered.
The model can choose among them but cannot invent one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

from harness.model.types import Message, ModelError, ModelRequest
from harness.orchestrator.protocol import ProtocolError, extract_json_object
from harness.repo.commands import CommandCandidate

MAX_TASK_CHARS = 8_000
MAX_ITEMS = 20
MAX_ITEM_CHARS = 300
MAX_UNDERSTANDING_CHARS = 1_000
STEP_KINDS = ("inspect", "edit", "test", "other")
PLANNER_MAX_OUTPUT_TOKENS = 2_000

PLAN_FIELDS = ("understanding", "acceptance_criteria", "hypotheses", "files_to_inspect", "steps",
               "verification_candidates", "risks")

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(PLAN_FIELDS),
    "properties": {
        "understanding": {"type": "string", "maxLength": MAX_UNDERSTANDING_CHARS},
        "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": {"type": "string"}},
        "hypotheses": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string"}},
        "files_to_inspect": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string"}},
        "steps": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "description"],
            "properties": {"kind": {"type": "string", "enum": list(STEP_KINDS)},
                           "description": {"type": "string"}}}},
        "verification_candidates": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string"},
                                    "description": "Only commands listed under 'Discovered commands'."},
        "risks": {"type": "array", "maxItems": MAX_ITEMS, "items": {"type": "string"}},
    },
}

PLANNER_INSTRUCTIONS = f"""You plan a change to a software repository. You cannot run anything in this step.
Reply with exactly one JSON object and nothing else. Fields (all required):
- understanding: one or two sentences restating the task.
- acceptance_criteria: 1-{MAX_ITEMS} short, checkable statements.
- hypotheses: likely causes or places to change (may be empty).
- files_to_inspect: repository-relative paths, most relevant first (may be empty).
- steps: 1-{MAX_ITEMS} objects {{"kind": one of {list(STEP_KINDS)}, "description": short imperative}}.
- verification_candidates: commands copied exactly from "Discovered commands" (may be empty; never invent one).
- risks: short statements (may be empty).
Keep every string under {MAX_ITEM_CHARS} characters (understanding under {MAX_UNDERSTANDING_CHARS}).
Write operational decisions, not deliberation."""


class PlanError(Exception):
    """The planner output was rejected. The message is safe to show."""


@dataclass(frozen=True)
class PlanStep:
    kind: str
    description: str


@dataclass(frozen=True)
class TaskPlan:
    understanding: str
    acceptance_criteria: tuple[str, ...]
    hypotheses: tuple[str, ...]
    files_to_inspect: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    verification_candidates: tuple[CommandCandidate, ...]   # resolved from discovered commands
    risks: tuple[str, ...]

    def render(self) -> str:
        lines = [f"Understanding: {self.understanding}", "Acceptance criteria:"]
        lines += [f"- {c}" for c in self.acceptance_criteria]
        if self.hypotheses:
            lines += ["Hypotheses:"] + [f"- {h}" for h in self.hypotheses]
        if self.files_to_inspect:
            lines.append("Files to inspect: " + ", ".join(self.files_to_inspect))
        lines += ["Steps:"] + [f"{i}. [{s.kind}] {s.description}" for i, s in enumerate(self.steps, start=1)]
        lines.append("Verification candidates: " + ("; ".join(" ".join(c.argv) for c in self.verification_candidates)
                                                    or "none (no command was discovered or chosen)"))
        if self.risks:
            lines += ["Risks:"] + [f"- {r}" for r in self.risks]
        return "\n".join(lines)


def command_text(candidate: CommandCandidate) -> str:
    return " ".join(candidate.argv)


def _string(value: Any, name: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PlanError(f"'{name}' must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise PlanError(f"'{name}' must not be empty")
    if len(value) > limit:
        raise PlanError(f"'{name}' is longer than {limit} characters")
    return value.strip()


def _string_list(value: Any, name: str, *, min_items: int = 0) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanError(f"'{name}' must be a list, got {type(value).__name__}")
    if len(value) < min_items:
        raise PlanError(f"'{name}' needs at least {min_items} item(s)")
    if len(value) > MAX_ITEMS:
        raise PlanError(f"'{name}' has more than {MAX_ITEMS} items")
    return tuple(_string(v, f"{name}[{i}]", MAX_ITEM_CHARS) for i, v in enumerate(value))


def parse_plan(text: str, discovered: Sequence[CommandCandidate],
               aliases: Optional[Mapping[str, CommandCandidate]] = None) -> TaskPlan:
    """``aliases`` maps other spellings of a discovered command (e.g. the unresolved
    ``python -m ...`` shown in the repository summary) to that same candidate."""
    try:
        obj = extract_json_object(text)
    except ProtocolError as exc:
        raise PlanError(str(exc)) from None
    missing = [f for f in PLAN_FIELDS if f not in obj]
    if missing:
        raise PlanError(f"missing required field(s): {', '.join(missing)}")
    extra = sorted(set(obj) - set(PLAN_FIELDS))
    if extra:
        raise PlanError(f"unexpected field(s): {', '.join(extra)}")

    steps_raw = obj["steps"]
    if not isinstance(steps_raw, list):
        raise PlanError(f"'steps' must be a list, got {type(steps_raw).__name__}")
    if not 1 <= len(steps_raw) <= MAX_ITEMS:
        raise PlanError(f"'steps' needs 1-{MAX_ITEMS} items")
    steps = []
    for i, raw in enumerate(steps_raw):
        if not isinstance(raw, dict) or set(raw) != {"kind", "description"}:
            raise PlanError(f"steps[{i}] must be an object with exactly 'kind' and 'description'")
        kind = _string(raw["kind"], f"steps[{i}].kind", 20)
        if kind not in STEP_KINDS:
            raise PlanError(f"steps[{i}].kind must be one of {', '.join(STEP_KINDS)}")
        steps.append(PlanStep(kind, _string(raw["description"], f"steps[{i}].description", MAX_ITEM_CHARS)))

    by_text = {**(aliases or {}), **{command_text(c): c for c in discovered}}
    chosen = []
    for text_cmd in _string_list(obj["verification_candidates"], "verification_candidates"):
        if text_cmd not in by_text:
            raise PlanError(f"verification candidate {text_cmd!r} was not discovered; choose from: "
                            f"{', '.join(command_text(c) for c in discovered) or 'none available'}")
        chosen.append(by_text[text_cmd])

    return TaskPlan(
        understanding=_string(obj["understanding"], "understanding", MAX_UNDERSTANDING_CHARS),
        acceptance_criteria=_string_list(obj["acceptance_criteria"], "acceptance_criteria", min_items=1),
        hypotheses=_string_list(obj["hypotheses"], "hypotheses"),
        files_to_inspect=_string_list(obj["files_to_inspect"], "files_to_inspect"),
        steps=tuple(steps),
        verification_candidates=tuple(chosen),
        risks=_string_list(obj["risks"], "risks"),
    )


def cap_task(task: str) -> str:
    if len(task) <= MAX_TASK_CHARS:
        return task
    return task[:MAX_TASK_CHARS] + f"\n[... task truncated: {len(task) - MAX_TASK_CHARS} more characters ...]"


def build_planner_request(task: str, discovery, commands: Sequence[CommandCandidate]) -> ModelRequest:
    s = discovery.task_signals
    signal_lines = [f"{label}: {', '.join(values)}" for label, values in (
        ("paths", s.explicit_paths), ("identifiers", s.identifiers), ("keywords", s.keywords)) if values]
    command_lines = [f"- {command_text(c)}  [{c.kind}, {c.confidence}] {c.reason}" for c in commands]
    user = "\n\n".join([
        "# Task\n" + cap_task(task),
        "# Task signals\n" + ("\n".join(signal_lines) or "none"),
        "# Repository context (selected by deterministic discovery)\n" + discovery.working_set.render(),
        "# Discovered commands\n" + ("\n".join(command_lines) or "none: no test, build or lint command "
                                     "is supported by the repository's configuration"),
    ])
    return ModelRequest(
        messages=(Message("system", PLANNER_INSTRUCTIONS), Message("user", user)),
        response_schema=PLAN_SCHEMA,
        max_output_tokens=PLANNER_MAX_OUTPUT_TOKENS,
        temperature=0.0,
        purpose="plan",
    )


PLAN_ATTEMPTS = 2   # a rejected plan is answered once with the exact error (a new, counted model call)


class Planner:
    def __init__(self, model, attempts: int = PLAN_ATTEMPTS) -> None:
        self.model = model   # a MeteredModelClient, so every call is counted
        self.attempts = max(1, attempts)

    def create_plan(self, task: str, discovery, commands: Sequence[CommandCandidate],
                    aliases: Optional[Mapping[str, CommandCandidate]] = None) -> TaskPlan:
        """Raises ``PlanError`` for rejected output (after ``attempts`` tries) and ``ModelError`` for any
        failed model call (an unexpected adapter exception is wrapped, as the executor does)."""
        request = build_planner_request(task, discovery, commands)
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.model.generate(request)
            except ModelError:
                raise
            except Exception as exc:
                raise ModelError(f"model call raised {exc.__class__.__name__}: {exc}") from exc
            try:
                if response.tool_calls:
                    raise PlanError("the planner must answer with a JSON plan, not a tool call")
                return parse_plan(response.text, commands, aliases)
            except PlanError as exc:
                if attempt == self.attempts:
                    raise
                request = replace(request, messages=request.messages + (
                    Message("assistant", response.text[:4_000] or "(no text)"),
                    Message("user", f"That plan was rejected: {exc}. Reply with the corrected plan: exactly one "
                                    f"JSON object with the required fields and limits, and nothing else.")))
        raise AssertionError("unreachable")


def plan_to_json(plan: TaskPlan) -> str:
    """Canonical JSON form (used by tests and reports)."""
    return json.dumps({
        "understanding": plan.understanding,
        "acceptance_criteria": list(plan.acceptance_criteria),
        "hypotheses": list(plan.hypotheses),
        "files_to_inspect": list(plan.files_to_inspect),
        "steps": [{"kind": s.kind, "description": s.description} for s in plan.steps],
        "verification_candidates": [command_text(c) for c in plan.verification_candidates],
        "risks": list(plan.risks),
    }, indent=2)
