"""Model construction boundary: configuration -> ``ModelClient``.

Dispatch is by **adapter** (transport/protocol), never by model family or model
name. DeepSeek and Qwen served through an OpenAI-compatible endpoint differ only
in configuration (``model``, ``base_url``, optional headers); ``provider`` is a
family label that is passed through for reporting and never selects code.

    ADAPTERS = {"openai_compatible": ...}      # "anthropic_compatible", native clients: add here

Every adapter returns an object implementing ``ModelClient``; nothing outside
this module and ``harness.model.adapters`` knows which one.
"""

from __future__ import annotations

from typing import Callable

from harness.config import ModelSettings
from harness.model.client import ModelClient

AdapterFactory = Callable[[ModelSettings, str], ModelClient]


class UnsupportedProviderError(Exception):
    """The model configuration cannot produce a client. The message is safe to print."""


def _openai_compatible(settings: ModelSettings, api_key: str) -> ModelClient:
    from harness.model.adapters.openai_compatible import OpenAICompatibleClient

    missing = [name for name, value in (("AI_MODEL", settings.name), ("AI_BASE_URL", settings.base_url)) if not value]
    if missing:
        raise UnsupportedProviderError(
            f"The openai_compatible adapter needs {' and '.join(missing)} (the organizer-provided model id and "
            f"endpoint); they are not set.")
    return OpenAICompatibleClient(
        api_key=api_key, model=settings.name, base_url=settings.base_url, timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries, headers=settings.headers, provider=settings.provider,
        structured_output=settings.structured_output)


ADAPTERS: dict[str, AdapterFactory] = {
    "openai_compatible": _openai_compatible,
}


def create_model_client(settings: ModelSettings, api_key: str) -> ModelClient:
    if not settings.adapter:
        raise UnsupportedProviderError(
            "No model provider is configured: AI_MODEL_ADAPTER is not set (e.g. openai_compatible), and no "
            "default has been confirmed by the organizers yet. Set AI_MODEL_ADAPTER, AI_MODEL and AI_BASE_URL.")
    factory = ADAPTERS.get(settings.adapter)
    if factory is None:
        raise UnsupportedProviderError(
            f"Configured model adapter is not supported by this build: {settings.adapter!r} "
            f"(supported: {', '.join(sorted(ADAPTERS))}).")
    return factory(settings, api_key)
