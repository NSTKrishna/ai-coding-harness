## Model abstraction

The harness depends on a provider-neutral `ModelClient` interface.

Provider protocol compatibility is an implementation detail below that
interface.

`OpenAICompatibleClient` is one implementation of `ModelClient`; it is not the
model abstraction itself.

Current/future implementations may include:

- `OpenAICompatibleClient`
- `AnthropicCompatClient`
- native provider clients

DeepSeek and Qwen may use `OpenAICompatibleClient` when the deployment offered
by the evaluator exposes an OpenAI-compatible API. Their model family does not
architecturally imply a particular transport.

No orchestration, planning, execution, verification, recovery or context code
may depend directly on a provider adapter.