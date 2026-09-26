"""Harness configuration.

Precedence (highest first): process environment, then a ``.env`` file in the
current working directory, then the defaults below. An empty value counts as
unset. ``AI_API_KEY`` is the only required value and the only secret.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

# ---------------------------------------------------------------------------
# ORGANIZER-PRESCRIBED MODEL  (UNRESOLVED - fill in once the organizers confirm)
#
# The evaluation uses DeepSeek and Qwen models, but the exact endpoint(s), model
# ids, transport and whether one key covers both are not confirmed yet. Do not
# guess them. Set these defaults (or export the AI_MODEL_* variables) once known.
#
#   adapter   = transport/protocol:  "openai_compatible" (built in), others later
#   provider  = model family label:  e.g. "deepseek", "qwen"  (informational only;
#               it never selects the transport)
#   model     = model id sent to the endpoint
#   base_url  = endpoint root, e.g. ".../v1" ("/chat/completions" is appended)
# ---------------------------------------------------------------------------
DEFAULT_MODEL_ADAPTER: Optional[str] = None
DEFAULT_MODEL_PROVIDER: Optional[str] = None
DEFAULT_MODEL: Optional[str] = None
DEFAULT_BASE_URL: Optional[str] = None
DEFAULT_MODEL_TIMEOUT_SECONDS = 120
DEFAULT_MODEL_MAX_RETRIES = 2

DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_REPAIR_CYCLES = 3
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_MAX_MODEL_CALLS = 60
DEFAULT_MAX_TOOL_CALLS = 80
DEFAULT_MAX_VERIFICATION_COMMANDS = 3
DEFAULT_MAX_REPEATED_FAILURE_CYCLES = 2

API_KEY_VAR = "AI_API_KEY"
PROVIDER_VAR = "AI_MODEL_PROVIDER"
ADAPTER_VAR = "AI_MODEL_ADAPTER"
MODEL_TIMEOUT_VAR = "AI_MODEL_TIMEOUT_SECONDS"
MODEL_MAX_RETRIES_VAR = "AI_MODEL_MAX_RETRIES"
MODEL_HEADERS_VAR = "AI_MODEL_HEADERS"
STRUCTURED_OUTPUT_VAR = "AI_MODEL_STRUCTURED_OUTPUT"
STRUCTURED_OUTPUT_MODES = ("none", "json_object")
MODEL_VAR = "AI_MODEL"
BASE_URL_VAR = "AI_BASE_URL"
MAX_STEPS_VAR = "HARNESS_MAX_STEPS"
MAX_REPAIR_CYCLES_VAR = "HARNESS_MAX_REPAIR_CYCLES"
COMMAND_TIMEOUT_VAR = "HARNESS_COMMAND_TIMEOUT_SECONDS"
MAX_MODEL_CALLS_VAR = "HARNESS_MAX_MODEL_CALLS"
MAX_TOOL_CALLS_VAR = "HARNESS_MAX_TOOL_CALLS"
MAX_VERIFICATION_COMMANDS_VAR = "HARNESS_MAX_VERIFICATION_COMMANDS"
MAX_REPEATED_FAILURE_VAR = "HARNESS_MAX_REPEATED_FAILURE_CYCLES"
TARGETED_TESTS_VAR = "HARNESS_TARGETED_TESTS"
VERIFY_FULL_SUITE_VAR = "HARNESS_VERIFY_FULL_SUITE"
TELEMETRY_VAR = "HARNESS_TELEMETRY"
RUNS_DIR_VAR = "HARNESS_RUNS_DIR"

# Working-context limits for repository discovery (see ContextLimits).
CONTEXT_LIMIT_VARS = {
    "max_active_files": "HARNESS_MAX_ACTIVE_FILES",
    "max_candidates": "HARNESS_MAX_CANDIDATES",
    "max_evidence_items": "HARNESS_MAX_EVIDENCE_ITEMS",
    "max_snippet_lines": "HARNESS_MAX_SNIPPET_LINES",
    "max_context_chars": "HARNESS_MAX_CONTEXT_CHARS",
    "compaction_threshold_chars": "HARNESS_CONTEXT_COMPACTION_THRESHOLD",
}

REDACTED = "***"


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class ModelSettings:
    provider: Optional[str]                  # model family label (deepseek, qwen, ...): informational
    name: Optional[str]                      # model id sent to the endpoint
    base_url: Optional[str]
    adapter: Optional[str] = None            # transport: selects the ModelClient implementation
    timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MODEL_MAX_RETRIES          # transient transport failures only
    headers: tuple[tuple[str, str], ...] = ()             # extra HTTP headers (e.g. gateway routing)
    structured_output: str = "none"          # "json_object": ask the endpoint for JSON mode on planner calls


@dataclass(frozen=True)
class Limits:
    max_steps: int = DEFAULT_MAX_STEPS                # executor iterations (one model decision each)
    max_repair_cycles: int = DEFAULT_MAX_REPAIR_CYCLES
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS    # planner + executor, failed attempts included
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS      # registry dispatches, failed ones included
    max_verification_commands: int = DEFAULT_MAX_VERIFICATION_COMMANDS  # per baseline/verification round
    max_repeated_failure_cycles: int = DEFAULT_MAX_REPEATED_FAILURE_CYCLES  # same failure after N repairs -> stop
    targeted_tests: bool = True       # derive a targeted test command and run it before the broad suite
    verify_full_suite: bool = True    # also run the broad suite after a passing targeted test


@dataclass(frozen=True)
class ContextLimits:
    """Bounds for the working set a planner receives (repository discovery)."""

    max_active_files: int = 8        # files whose evidence may enter the working set
    max_candidates: int = 25         # ranked candidates listed in the working set
    max_evidence_items: int = 24     # snippets in the working set
    max_snippet_lines: int = 30      # lines per snippet
    max_context_chars: int = 24_000  # rendered working set, hard ceiling
    compaction_threshold_chars: int = 60_000   # executor request size above which context is compacted


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: ModelSettings
    limits: Limits
    env_file: Optional[Path] = None
    context: ContextLimits = field(default_factory=ContextLimits)
    telemetry_enabled: bool = True    # write run artifacts (see harness.telemetry)
    runs_dir: Optional[Path] = None   # artifact root override; None = harness default location

    def redact(self, text: str) -> str:
        """Replace every occurrence of the API key in ``text``."""
        return text.replace(self.api_key, REDACTED) if self.api_key else text


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Supports comments, ``export`` and quotes.

    Error messages carry line numbers only, never line content, because a
    malformed line may contain a secret.
    """
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ConfigError(f".env line {lineno} is not a KEY=VALUE assignment")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _settings(
    environ: Optional[Mapping[str, str]], dotenv_path: Optional[Path]
) -> tuple[Callable[[str], Optional[str]], Optional[Path]]:
    """Return a lookup (environment first, then ``.env``) and the ``.env`` path used."""
    env = os.environ if environ is None else environ
    if dotenv_path is None:
        dotenv_path = Path.cwd() / ".env"

    dotenv: dict[str, str] = {}
    env_file: Optional[Path] = None
    if dotenv_path.is_file():
        try:
            dotenv = parse_dotenv(dotenv_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read {dotenv_path}: {exc.__class__.__name__}") from None
        env_file = dotenv_path

    def get(name: str) -> Optional[str]:
        for source in (env, dotenv):
            value = (source.get(name) or "").strip()
            if value:
                return value
        return None

    return get, env_file


def load_context_limits(
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
) -> ContextLimits:
    """Context limits only. Needs no API key (used by ``harness inspect``)."""
    get, _ = _settings(environ, dotenv_path)
    return _context_limits(get)


def _context_limits(get: Callable[[str], Optional[str]]) -> ContextLimits:
    defaults = ContextLimits()
    return ContextLimits(**{
        name: _positive_int(var, get(var), getattr(defaults, name))
        for name, var in CONTEXT_LIMIT_VARS.items()
    })


def load_config(
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
) -> Config:
    """Build a validated ``Config``. Raises ``ConfigError`` with a usable message."""
    get, env_file = _settings(environ, dotenv_path)

    api_key = get(API_KEY_VAR)
    if api_key is None:
        raise ConfigError(
            f"{API_KEY_VAR} is not set.\n"
            f"Export it before running:\n"
            f'    export {API_KEY_VAR}="<your key>"\n'
            f"or put it in a .env file in {Path.cwd()} (see .env.example)."
        )

    base_url = get(BASE_URL_VAR) or DEFAULT_BASE_URL
    if base_url is not None and not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{BASE_URL_VAR} must start with http:// or https:// (got {base_url!r})")

    return Config(
        api_key=api_key,
        model=ModelSettings(
            provider=get(PROVIDER_VAR) or DEFAULT_MODEL_PROVIDER,
            name=get(MODEL_VAR) or DEFAULT_MODEL,
            base_url=base_url,
            adapter=get(ADAPTER_VAR) or DEFAULT_MODEL_ADAPTER,
            timeout_seconds=_positive_int(MODEL_TIMEOUT_VAR, get(MODEL_TIMEOUT_VAR), DEFAULT_MODEL_TIMEOUT_SECONDS),
            max_retries=_positive_int(MODEL_MAX_RETRIES_VAR, get(MODEL_MAX_RETRIES_VAR), DEFAULT_MODEL_MAX_RETRIES,
                                      allow_zero=True),
            headers=_headers(get(MODEL_HEADERS_VAR)),
            structured_output=_choice(STRUCTURED_OUTPUT_VAR, get(STRUCTURED_OUTPUT_VAR), STRUCTURED_OUTPUT_MODES, "none"),
        ),
        limits=Limits(
            max_steps=_positive_int(MAX_STEPS_VAR, get(MAX_STEPS_VAR), DEFAULT_MAX_STEPS),
            max_repair_cycles=_positive_int(
                MAX_REPAIR_CYCLES_VAR, get(MAX_REPAIR_CYCLES_VAR), DEFAULT_MAX_REPAIR_CYCLES, allow_zero=True
            ),  # 0 = verify but never repair
            command_timeout_seconds=_positive_int(
                COMMAND_TIMEOUT_VAR, get(COMMAND_TIMEOUT_VAR), DEFAULT_COMMAND_TIMEOUT_SECONDS
            ),
            max_model_calls=_positive_int(MAX_MODEL_CALLS_VAR, get(MAX_MODEL_CALLS_VAR), DEFAULT_MAX_MODEL_CALLS),
            max_tool_calls=_positive_int(MAX_TOOL_CALLS_VAR, get(MAX_TOOL_CALLS_VAR), DEFAULT_MAX_TOOL_CALLS),
            max_verification_commands=_positive_int(MAX_VERIFICATION_COMMANDS_VAR, get(MAX_VERIFICATION_COMMANDS_VAR),
                                                    DEFAULT_MAX_VERIFICATION_COMMANDS),
            max_repeated_failure_cycles=_positive_int(MAX_REPEATED_FAILURE_VAR, get(MAX_REPEATED_FAILURE_VAR),
                                                      DEFAULT_MAX_REPEATED_FAILURE_CYCLES),
            targeted_tests=_bool(TARGETED_TESTS_VAR, get(TARGETED_TESTS_VAR), True),
            verify_full_suite=_bool(VERIFY_FULL_SUITE_VAR, get(VERIFY_FULL_SUITE_VAR), True),
        ),
        env_file=env_file,
        context=_context_limits(get),
        telemetry_enabled=_bool(TELEMETRY_VAR, get(TELEMETRY_VAR), True),
        runs_dir=Path(get(RUNS_DIR_VAR)).expanduser() if get(RUNS_DIR_VAR) else None,
    )


def load_runtime_settings(
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
) -> tuple[bool, Optional[Path]]:
    """(telemetry_enabled, runs_dir) without requiring the API key (for `harness runs/report`)."""
    get, _ = _settings(environ, dotenv_path)
    runs_dir = get(RUNS_DIR_VAR)
    return _bool(TELEMETRY_VAR, get(TELEMETRY_VAR), True), Path(runs_dir).expanduser() if runs_dir else None


def _headers(raw: Optional[str]) -> tuple[tuple[str, str], ...]:
    """AI_MODEL_HEADERS: a JSON object of extra HTTP headers. Values are never printed."""
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError(f"{MODEL_HEADERS_VAR} must be a JSON object of header names to string values") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ConfigError(f"{MODEL_HEADERS_VAR} must be a JSON object of header names to string values")
    if any(k.lower() == "authorization" for k in data):
        raise ConfigError(f"{MODEL_HEADERS_VAR} must not set Authorization; the key comes from {API_KEY_VAR}")
    return tuple(sorted(data.items()))


def _choice(name: str, raw: Optional[str], allowed: tuple[str, ...], default: str) -> str:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)} (got {raw!r})")
    return value


def _bool(name: str, raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false (got {raw!r})")


def _positive_int(name: str, raw: Optional[str], default: int, *, allow_zero: bool = False) -> int:
    what = "a non-negative integer" if allow_zero else "a positive integer"
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {what} (got {raw!r})") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} must be {what} (got {raw!r})")
    return value
