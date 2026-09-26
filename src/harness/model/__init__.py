"""Provider-neutral model interface.

No live provider adapter exists yet: the hackathon has not announced the
provider, model or endpoint. Adapters will implement ``ModelClient`` and map
provider payloads to the types in ``harness.model.types``.
"""

from harness.model.client import MeteredModelClient, ModelClient
from harness.model.fake import (
    ScriptedModel,
    ScriptExhausted,
    malformed_tool_call_response,
    text_response,
    tool_call_response,
)
from harness.model.types import (
    FinishReason,
    Message,
    ModelError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    Usage,
)

__all__ = [
    "FinishReason",
    "Message",
    "MeteredModelClient",
    "ModelClient",
    "ModelError",
    "ModelRequest",
    "ModelResponse",
    "ScriptExhausted",
    "ScriptedModel",
    "ToolCall",
    "ToolDefinition",
    "Usage",
    "malformed_tool_call_response",
    "text_response",
    "tool_call_response",
]
