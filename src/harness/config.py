"""Harness configuration.

Precedence (highest first): process environment, then a ``.env`` file in the
current working directory, then the defaults below. An empty value counts as
unset. ``AI_API_KEY`` is the only required value and the only secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

# ---------------------------------------------------------------------------
# ORGANIZER-PRESCRIBED MODEL
#
# The hackathon has not announced a provider, model or endpoint yet. When it
# does, set these defaults here (or export AI_MODEL_PROVIDER / AI_MODEL /
# AI_BASE_URL). Do not guess them.
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PROVIDER: Optional[str] = None
DEFAULT_MODEL: Optional[str] = None
DEFAULT_BASE_URL: Optional[str] = None

DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_REPAIR_CYCLES = 3
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300

API_KEY_VAR = "AI_API_KEY"
PROVIDER_VAR = "AI_MODEL_PROVIDER"
MODEL_VAR = "AI_MODEL"
BASE_URL_VAR = "AI_BASE_URL"
MAX_STEPS_VAR = "HARNESS_MAX_STEPS"
MAX_REPAIR_CYCLES_VAR = "HARNESS_MAX_REPAIR_CYCLES"
COMMAND_TIMEOUT_VAR = "HARNESS_COMMAND_TIMEOUT_SECONDS"

REDACTED = "***"


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class ModelSettings:
    provider: Optional[str]
    name: Optional[str]
    base_url: Optional[str]


@dataclass(frozen=True)
class Limits:
    max_steps: int
    max_repair_cycles: int
    command_timeout_seconds: int


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: ModelSettings
    limits: Limits
    env_file: Optional[Path] = None

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


def load_config(
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
) -> Config:
    """Build a validated ``Config``. Raises ``ConfigError`` with a usable message."""
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
        ),
        limits=Limits(
            max_steps=_positive_int(MAX_STEPS_VAR, get(MAX_STEPS_VAR), DEFAULT_MAX_STEPS),
            max_repair_cycles=_positive_int(
                MAX_REPAIR_CYCLES_VAR, get(MAX_REPAIR_CYCLES_VAR), DEFAULT_MAX_REPAIR_CYCLES
            ),
            command_timeout_seconds=_positive_int(
                COMMAND_TIMEOUT_VAR, get(COMMAND_TIMEOUT_VAR), DEFAULT_COMMAND_TIMEOUT_SECONDS
            ),
        ),
        env_file=env_file,
    )


def _positive_int(name: str, raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a positive integer (got {raw!r})") from None
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer (got {raw!r})")
    return value
