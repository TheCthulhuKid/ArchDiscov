# Assistant provider contracts

This page describes the provider-neutral layer under `src/archdiscov/assistant/`: the
conversation and tool-call contracts, provider errors and capabilities, the `Provider`
protocol, provider configuration, and the scripted fake provider used to test everything
above it. The `ask` command builds its provider from a configuration file, and the only
provider that answers is the fake; the [Anthropic adapter](#anthropic-adapter) is being
added. The [assistant tools](assistant-tools.md), [orchestration](assistant-orchestration.md),
and [answers](assistant-answers.md) build on this layer. Only an adapter module imports a
vendor SDK, and only when its kind is built.

## Conversation contracts

`assistant/conversation.py` models the lowest common shape of the major LLM message
formats. Every contract is frozen and rejects unknown fields; vendor-specific fields
belong to adapters.

| Contract | Holds |
|---|---|
| `SystemMessage`, `UserMessage` | `text` |
| `AssistantMessage` | `text`, `tool_calls`, or both; never neither |
| `ToolResultMessage` | one or more `ToolResult`s, such as the results of one parallel turn |
| `ToolDeclaration` | `name`, `description`, and an `input_schema` describing a JSON object |
| `ToolCall` | provider-assigned `id`, `name`, and `arguments` |
| `ToolResult` | `call_id`, text `content`, and an `is_error` flag |
| `TokenUsage` | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_write_input_tokens`, each optional |
| `Conversation` | the ordered `messages` |

A `Conversation` enforces structure every provider can represent: system messages come
first, tool call IDs are unique across the conversation, and each tool result answers
exactly one earlier call. Tool names start with a letter and contain only letters,
digits, and underscores, up to 64 characters.

Tool arguments are a value, not an exception: `parsed` holds a JSON object, and
`malformed` holds only a `reason` (`invalid_json`, `not_object`, or `too_large`), never
the raw text, so the orchestrator can answer the model with a tool error.
`parse_tool_arguments` classifies provider text or decoded values without raising;
duplicate keys and non-finite numbers are invalid JSON.

`parse_conversation` validates detached data and reports only stable codes and known
field names, never input text or arguments.

| Bound | Value | Applies to |
|---|---|---|
| `MAX_TEXT_CHARS` | 200,000 | characters in message text |
| `MAX_TOOL_DESCRIPTION_CHARS` | 4,096 | characters in a tool description |
| `MAX_TOOL_INPUT_SCHEMA_BYTES` | 32,768 | bytes of a tool input schema as compact JSON |
| `MAX_TOOL_ARGUMENT_BYTES` | 16,384 | bytes of tool arguments, as text or compact JSON |
| `MAX_TOOL_RESULT_CHARS` | 262,144 | characters in tool result content |
| `MAX_TOOL_CALL_ID_CHARS` | 256 | characters in a tool call ID |

## Provider protocol

`assistant/provider.py` defines one operation in sync and async form:
`complete(request)` and `complete_async(request)` take a `CompletionRequest` and return a
`CompletionResponse`. A provider also exposes its `capabilities`. There is no streaming,
caching, or conversation state.

- A `CompletionRequest` holds the `conversation`, the `tools` offered (names unique),
  and per-request `limits`.
- A `CompletionResponse` holds the assistant `message`, its `usage`, and a `stop_reason`:
  `end`, `tool_use`, `length`, or `refusal`. `tool_use` is used exactly when the
  message has tool calls. `length` and `refusal` carry whatever text the provider
  returned; a provider that returned nothing usable raises the matching error instead.

| Request limit | Default | Allowed |
|---|---|---|
| `max_output_tokens` | 4,096 | 1–128,000 |
| `timeout_seconds` | 60 | >0–600 |
| `max_tool_calls_per_turn` | 1 | 1–32 |

Run-wide limits (total requests, tool calls, and latency) are configured below and
enforced by the orchestrator.

### Capabilities

A provider declares any of `tool_calling`, `parallel_tool_calls` (which requires
`tool_calling`), and `structured_output`. A request needs `tool_calling` when it offers
tools or its conversation already contains tool calls or results, and
`parallel_tool_calls` when it allows more than one call per turn. The module-level
`complete` and `complete_async` run `check_capabilities` first, so a request needing an
unsupported capability fails with `provider_unsupported_capability` before any provider
call, rather than being degraded silently. No request requires `structured_output` yet.

### Errors and the retry boundary

Every provider failure is a `ProviderError` subclass with a stable code, a fixed
message, a `retryable` flag, and `diagnostic()`. Messages never include keys, request
bodies, provider responses, or tool-result content; adapters chain the underlying
exception for local debugging only.

| Code | Error | Retryable | Message |
|---|---|---|---|
| `provider_authentication` | `ProviderAuthenticationError` | no | Provider rejected the configured credentials. |
| `provider_rate_limit` | `ProviderRateLimitError` | yes | Provider rate limit reached. |
| `provider_timeout` | `ProviderTimeoutError` | no | Provider request timed out. |
| `provider_transport` | `ProviderTransportError` | yes | Provider could not be reached. |
| `provider_invalid_request` | `ProviderInvalidRequestError` | no | Provider rejected the request. |
| `provider_malformed_output` | `ProviderMalformedOutputError` | no | Provider returned output that does not fit the conversation contracts. |
| `provider_unsupported_capability` | `UnsupportedCapabilityError` | no | Provider does not support a capability the request needs. |
| `provider_truncated_output` | `ProviderTruncatedOutputError` | no | Provider output was truncated before a usable message. |
| `provider_refusal` | `ProviderRefusalError` | no | Provider refused to answer. |
| `provider_unavailable` | `ProviderUnavailableError` | no | Provider is unavailable. |

The retry boundary is fixed: a provider retries only retryable errors (transport and
rate limit), up to its configured `max_retries`, inside one `complete` call. Nothing
retries a rejected answer; an invalid answer gets at most one repair request from the
answer renderer in a later step.

## Configuration

`assistant/config.py` loads a TOML file of at most 64 KiB (`MAX_CONFIG_BYTES`) with
`load_provider_config(path)`, or validates data with `parse_provider_config`. The file
is one table selected by `kind`. The `ask` command reads it with `--config`;
the fake is the only provider that answers, and the other kinds define the configuration
their adapters use.

```toml
kind = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"   # the variable's name, never its value
max_tool_calls_per_turn = 4
request_timeout_seconds = 30
```

| Field | Kinds | Default | Allowed |
|---|---|---|---|
| `kind` | all | required | `fake`, `anthropic`, `openai_compatible`, `bedrock` |
| `model` | `fake` | `fake` | non-blank, up to 256 characters |
| `transcript` | `fake` | none | a fake transcript file, relative to the configuration file or absolute; required to build the provider |
| `model` | `anthropic`, `openai_compatible`, `bedrock` | required | non-blank, up to 256 characters |
| `connect_timeout_seconds` | all | 10 | >0–60 |
| `request_timeout_seconds` | all | 60 | >0–600, at most `max_latency_seconds` |
| `max_retries` | all | 2 | 0–5 |
| `max_output_tokens` | all | 4,096 | 1–128,000 |
| `max_requests` | all | 8 | 1–50 |
| `max_tool_calls` | all | 32 | 1–200 |
| `max_tool_calls_per_turn` | all | 1 | 1–32, at most `max_tool_calls` |
| `max_latency_seconds` | all | 300 | >0–3,600 |
| `api_key_env` | `anthropic`, `openai_compatible` | required | environment variable name: `A`–`Z`, digits, `_` |
| `endpoint` | `anthropic` | none | HTTPS URL |
| `endpoint` | `openai_compatible` | required | HTTPS URL |
| `protocol` | `openai_compatible` | required | `responses`, `chat_completions` |
| `profile` | `bedrock` | required | AWS profile name |
| `region` | `bedrock` | required | a concrete AWS region, not `global` |
| `role_arn` | `bedrock` | none | IAM role ARN |
| `session_name` | `bedrock` | `archdiscov-assistant` | non-blank |

Numbers are strictly typed: `"5"` or `true` is not a number. Endpoints may use HTTP only
for loopback hosts (`localhost`, `127.0.0.1`, `::1`), and never carry user info, a query,
or a fragment. `request_limits()` turns a configuration into the per-request limits
above; the remaining fields bound the whole run.

### Building providers

`build_provider(config, config_dir=)` in `assistant/providers.py` turns a configuration into
a provider through `ADAPTERS`, a table keyed by kind, so each real adapter (steps 40-42)
plugs in there. It resolves the referenced credential and passes it only to the adapter.
`load_provider(path)` loads a file and builds its provider, resolving relative paths
against the file's directory.

- The fake needs a `transcript`, loaded with the fake transcript limits; without one the
  build fails with `provider_transcript_missing`, and an unreadable or invalid transcript
  fails with the fake's own error codes.
- `anthropic` builds the [Anthropic adapter](#anthropic-adapter).
- A kind without an adapter (`openai_compatible` and `bedrock`) fails with
  `provider_adapter_unavailable`, before its credential is read or any request is made.

Errors name the file and the kind, never the configuration's values or credentials.

### Anthropic adapter

`assistant/anthropic_provider.py` is the only module that imports the `anthropic` SDK,
and `ADAPTERS` imports it only when an `anthropic` configuration is built. Building it
makes no request. The client comes only from the configuration:

- The key from `api_key_env` and the base URL (`endpoint`, the API root without `/v1`, or
  `https://api.anthropic.com`) are passed explicitly, so the SDK's own environment
  credentials, profiles, workload identity, and `ANTHROPIC_BASE_URL` are never used.
- Every request passes through a transport that keeps only the SDK's own headers and then
  sets the configured key, so `ANTHROPIC_CUSTOM_HEADERS` cannot add headers or replace the
  credential. A client with its own transport uses no environment proxy.
- Redirects are not followed, the SDK's own retries are off (`max_retries = 0`; the
  adapter's retries arrive with its error mapping), and the timeouts are
  `connect_timeout_seconds` and `request_timeout_seconds`.
- OpenTelemetry spans are off, and the `anthropic` and `httpx2` loggers are held at
  `WARNING` whatever `ANTHROPIC_LOG` or the root level says, because their debug records
  hold request bodies.

The adapter declares tool calling and parallel tool calls. Its request and response
mapping is not in place yet, so every request fails with `provider_unavailable` before
anything is sent.

### Credentials

- Credentials are references only: `api_key_env` names an environment variable, and
  Bedrock uses its own AWS `profile`, separate from collection credentials.
- API keys are never accepted as CLI arguments, which leak into shell history and
  process listings, or as configuration values.
- Before field validation, the whole file is scanned: credential-like field names
  (such as `api_key`, `password`, `token`, or `authorization`, at any depth) and
  secret-looking values (API-key prefixes, AWS access key IDs, bearer tokens, private
  keys, and long mixed-case opaque tokens) are rejected.
- `resolve_api_key(config)` reads the variable only when a provider is constructed, and
  fails with `provider_credential_missing` or `provider_credential_empty` before any
  request. It returns a `SecretStr`, which `repr`, `str`, and JSON dumps mask;
  configurations never hold resolved values.

`ProviderConfigError` carries a code (`provider_config_unreadable`,
`provider_config_too_large`, `provider_config_malformed`, `provider_config_invalid`,
`provider_credential_missing`, or `provider_credential_empty`), a fixed message, the
file path, the referenced variable name, and code-only issue details, never file
contents or field values.

## Fake provider

`assistant/fake.py` replays a strict JSON transcript of at most 1 MiB
(`MAX_TRANSCRIPT_BYTES`), loaded with `load_fake_transcript(path)`. It is the reference
behaviour for orchestration tests and the scenario format for the shared provider
contract suite.

```json
{
  "capabilities": {"capabilities": ["tool_calling", "parallel_tool_calls"]},
  "turns": [
    {
      "kind": "respond",
      "expect": {"tool_names": ["search_resources"], "last_message_role": "user"},
      "response": {
        "message": {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "search_resources",
              "arguments": {"kind": "parsed", "value": {"type": "rds_instance"}}
            }
          ]
        },
        "usage": {"input_tokens": 120, "output_tokens": 18},
        "stop_reason": "tool_use"
      }
    },
    {"kind": "fail", "error": "provider_transport", "delay_seconds": 0.5}
  ]
}
```

- `capabilities` defaults to tool calling and parallel tool calls; a turn whose tool
  calls need an undeclared capability is invalid.
- A `respond` turn returns its `response`; a `fail` turn raises the provider error for
  `error`, any code except `provider_unsupported_capability`.
- Any turn may add `delay_seconds` (0–600) and an `expect` with the offered tool names,
  in order, and the role of the last message. `ignore_request_limits` lets a `respond`
  turn model a provider that ignores the request: more tool calls or output tokens than
  requested, or a delay past the request timeout without timing out.

Each attempt consumes the next turn. Running out of turns, an unmet expectation, or a
response with more tool calls or output tokens than the request allows raises
`FakeScriptError`, a test failure that is deliberately not a `ProviderError`. Like a real
adapter, the fake checks capabilities before consuming anything, retries only retryable
errors up to `max_retries` (each retry consumes a turn), and turns a delay beyond the
request timeout into `provider_timeout`. Delays advance an injected `FakeClock`, so
nothing waits, and every attempt is recorded in `requests` with the clock reading.

Example transcripts are in `tests/fixtures/assistant/`. The shared contract suite in
`tests/assistant/test_provider_contract.py` runs every provider harness in
`tests/assistant/provider_harness.py` through the same mapping, error, capability,
retry, and timeout cases.
