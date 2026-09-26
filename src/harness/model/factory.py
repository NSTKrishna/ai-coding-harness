"""Provider construction boundary.

No live adapter exists in this build: the hackathon has not announced the
provider, model or endpoint. ``ADAPTERS`` maps a provider name to a
constructor ``(settings, api_key) -> ModelClient`` and is intentionally empty.
Adding the prescribed provider means adding one entry here; nothing else in
the harness depends on which provider it is.
"""

from __future__ import annotations

from typing import Callable

from harness.config import ModelSettings
from harness.model.client import ModelClient

AdapterFactory = Callable[[ModelSettings, str], ModelClient]
ADAPTERS: dict[str, AdapterFactory] = {}


class UnsupportedProviderError(Exception):
    """No adapter for the configured provider. The message is safe to print."""


def create_model_client(settings: ModelSettings, api_key: str) -> ModelClient:
    if not settings.provider:
        raise UnsupportedProviderError(
            "No model provider is configured (AI_MODEL_PROVIDER is not set). The hackathon has not "
            "announced the prescribed provider yet, and this build contains no live model adapter."
        )
    factory = ADAPTERS.get(settings.provider)
    if factory is None:
        supported = ", ".join(sorted(ADAPTERS)) or "none yet"
        raise UnsupportedProviderError(
            f"Configured model provider is not supported by this build: {settings.provider!r} "
            f"(supported: {supported})."
        )
    return factory(settings, api_key)
