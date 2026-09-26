"""The model interface the rest of the harness depends on."""

from __future__ import annotations

from typing import Protocol

from harness.metrics import ExecutionMetrics
from harness.model.types import ModelRequest, ModelResponse


class ModelClient(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class MeteredModelClient:
    """Wraps any ``ModelClient`` and counts every call, including failed ones."""

    def __init__(self, inner: ModelClient, metrics: ExecutionMetrics) -> None:
        self.inner = inner
        self.metrics = metrics

    def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            response = self.inner.generate(request)
        except Exception as exc:
            self.metrics.record_model_call(failed=True, attempts=getattr(exc, "attempts", 1))
            raise
        self.metrics.record_model_call(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            attempts=int(response.metadata.get("provider_attempts", 1)),
        )
        return response
