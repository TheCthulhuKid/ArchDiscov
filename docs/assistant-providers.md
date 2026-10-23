# Assistant provider contracts

This page describes the provider-neutral layer under `src/archdiscov/assistant/`: the
conversation and tool-call contracts, provider errors and capabilities, the `Provider`
protocol, provider configuration, and the scripted fake provider used to test everything
above it. The `ask` command builds its provider from a configuration file: the fake, the
[Anthropic adapter](#anthropic-adapter), or the
[OpenAI-compatible adapter](#openai-compatible-adapter), which does not answer yet;
`bedrock` has no adapter yet. The
[assistant tools](assistant-tools.md), [orchestration](assistant-orchestration.md), and
[answers](assistant-answers.md) build on this layer. Only an adapter module imports a
vendor SDK, and only when its kind is built.

## Conversation contracts

`assistant/conversation.py` models the lowest common shape of the major LLM message
formats. Every contract is frozen and rejects unknown fields; vendor-specific fields
belong to adapters.

| Contract | Holds |
|---|---|
| `SystemMessage`, `UserMessage` | `text` |
| `AssistantMessage` | `text`, `tool_calls`, or both; never neither; and an optional `continuation` |
| `ProviderContinuation` | `provider_kind` and one or more opaque JSON `blocks` |
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

A `ProviderContinuation` holds blocks one provider needs back unchanged with its own
message, such as the reasoning blocks current Claude models return and expect again in a
tool loop. Only the adapter for its `provider_kind` reads it: it is not model output, so
answer validation, citations, renderings, and tools never see it. The orchestrator replays
it with the message, ends the run with `continuation_foreign` if a response carries another
provider's continuation, counts its JSON size toward the total-output limit, and records
only that size in the transcript. The fake provider returns a scripted continuation as-is.

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
| `MAX_CONTINUATION_BYTES` | 262,144 | bytes of a continuation's blocks as compact JSON |

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
An adapter that cannot ask its API what a model supports, such as the
[OpenAI-compatible adapter](#openai-compatible-adapter), declares what its configuration
says.

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
the fake, `anthropic`, and `openai_compatible` have adapters, and `bedrock` defines the
configuration its adapter will use.

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
| `capabilities` | `openai_compatible` | `tool_calling` | a list of `tool_calling`, `parallel_tool_calls` (which requires `tool_calling`), `structured_output` |
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
- `openai_compatible` builds the [OpenAI-compatible adapter](#openai-compatible-adapter).
- A kind without an adapter (`bedrock`) fails with `provider_adapter_unavailable`, before
  its credential is read or any request is made.

Errors name the file and the kind, never the configuration's values or credentials.

### Anthropic adapter

`assistant/anthropic_provider.py` is the only module that imports the `anthropic` SDK,
and `ADAPTERS` imports it only when an `anthropic` configuration is built. Building it
makes no request.

```toml
kind = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
endpoint = "https://llm-gateway.example.com/anthropic"   # optional: a gateway's API root
```

Without `endpoint`, requests go to `https://api.anthropic.com`. A gateway at `endpoint`
receives `POST {endpoint}/v1/messages` with the key as `Authorization: Bearer`, which the
SDK sends in place of `x-api-key`, so a gateway that accepts only `x-api-key` is not
supported. Plain HTTP is accepted only for loopback hosts, such as a local mock server.

The client comes only from the configuration. The guarded clients, logger quieting, retry
loop, and the response rules every protocol shares live in `assistant/adapters.py`, which
imports no vendor SDK, so each adapter supplies only its SDK's header names, exception
table, and mapping:

- The key from `api_key_env` and the base URL (`endpoint`, the API root without `/v1`, or
  `https://api.anthropic.com`) are passed explicitly, so the SDK's own environment
  credentials, profiles, workload identity, and `ANTHROPIC_BASE_URL` are never used.
- Every request passes through a transport that keeps only the SDK's own headers and then
  sets the configured key, so `ANTHROPIC_CUSTOM_HEADERS` cannot add headers or replace the
  credential. A client with its own transport uses no environment proxy.
- Redirects are not followed, the SDK's own retries are off (`max_retries = 0`; the
  adapter retries, as described under [failures](#failures-and-retries)), and the timeouts
  are `connect_timeout_seconds` and `request_timeout_seconds`.
- OpenTelemetry spans are off, and the `anthropic` and `httpx2` loggers are held at
  `WARNING` whatever `ANTHROPIC_LOG` or the root level says, because their debug records
  hold request bodies.

The adapter declares tool calling and parallel tool calls without asking the API, since
every Messages API model supports tool use; a model the API does not know is refused by its
response, as `provider_invalid_request`.

Each request becomes one `messages.create` call:

- System messages become `system`. User, assistant, and tool-result messages follow in
  order, and consecutive user-role messages (tool results and a repair prompt) share one
  turn. Tool calls become `tool_use` blocks (a call with malformed arguments, which never
  ran, is replayed with an empty input) and tool results become `tool_result` blocks with
  `is_error`.
- Tools become `name`, `description`, and `input_schema`, with `tool_choice` `auto` and
  `disable_parallel_tool_use` when `max_tool_calls_per_turn` is 1. `max_tokens` is
  `max_output_tokens`, and the request's timeout is passed with it, which also keeps the
  SDK from requiring streaming for a large `max_tokens`.
- No sampling parameters, forced tool choice, prefill, or thinking settings are sent; the
  model's defaults apply.

The response is untrusted. Text blocks are joined into the message text and `tool_use`
blocks become tool calls; an input that arrives as a string is parsed as JSON text, and any
other non-object input is a malformed-arguments value. Any other block type, including
server tools, is `provider_malformed_output`, as is a response that does not fit the
contracts. Usage maps input, output, cache read, and cache creation (as cache write) tokens.

| `stop_reason` | Maps to |
|---|---|
| `end_turn`, `stop_sequence` | `end` |
| `tool_use` | `tool_use` |
| `max_tokens`, `model_context_window_exceeded` | `length` for text alone; otherwise `provider_truncated_output` |
| `refusal` | `refusal` for text alone; otherwise `provider_refusal` |
| any other, such as `pause_turn` | `provider_malformed_output` |

Current Claude models return `thinking` and `redacted_thinking` blocks that must go back
unchanged and in their original order. A response holding them keeps its whole content, in
order, as an `anthropic` continuation, and the next request replays that content verbatim;
a response without them is rebuilt from its text and tool calls. A continuation from
another provider kind is refused as `provider_invalid_request` before anything is sent.

#### Failures and retries

A failure maps to a provider error by the SDK's exception class, never by message text,
through the class hierarchy, so a subclass maps before its base. The error has its fixed
message and chains the SDK exception for local debugging, copying none of its text,
headers, or request ID.

| SDK exception | Status | Provider error |
|---|---|---|
| `BadRequestError` | 400 | `provider_invalid_request` |
| `NotFoundError` | 404, such as an unknown model | `provider_invalid_request` |
| `RequestTooLargeError` | 413 | `provider_invalid_request` |
| `UnprocessableEntityError` | 422 | `provider_invalid_request` |
| `AuthenticationError` | 401 | `provider_authentication` |
| `PermissionDeniedError` | 403 | `provider_authentication` |
| `RateLimitError` | 429 | `provider_rate_limit` |
| `OverloadedError` | 529 | `provider_rate_limit` |
| `InternalServerError` | other 5xx | `provider_unavailable` |
| `ServiceUnavailableError` | 503 | `provider_unavailable` |
| `DeadlineExceededError` | 504 | `provider_unavailable` |
| `APIStatusError` | any other status, such as a redirect | `provider_invalid_request` |
| `APITimeoutError` | none: the request timed out | `provider_timeout` |
| `APIConnectionError` | none: the API could not be reached | `provider_transport` |
| `APIResponseValidationError` | a response that does not fit the SDK | `provider_malformed_output` |
| `JSONDecodeError` | a success body that is not JSON | `provider_malformed_output` |
| `APIError` | any other SDK failure | `provider_unavailable` |

Only transport and rate-limit errors, overload included, are retried, up to the
configuration's `max_retries`, and a timeout never is. Before each retry the adapter waits
the response's `retry-after` seconds, or else `RETRY_BACKOFF_SECONDS` (0.5) doubled for each
earlier retry, never longer than `MAX_RETRY_WAIT_SECONDS` (30) or the request timeout. The
wait goes through an injected sleep, so tests never wait. Retries and waits count toward
the request's deadline in the [orchestrator](assistant-orchestration.md#limits).

#### Live smoke test

`tests/live/test_anthropic_live.py` asks one README question through the real Messages API.
It is marked `live`, which the default pytest options deselect (`-m "not live"`), so the
normal run and CI never collect it, and it skips unless two variables are set: one naming
the environment variable that holds the key, and one naming the model. It is capped at four
requests (plus the one repair) of 4,096 output tokens each and one retry, and it accepts a
validated answer or an explained failure. It needs a key, network access, and spends real
tokens, so run it only on purpose:

```sh
ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV=ANTHROPIC_API_KEY \
ARCHDISCOV_LIVE_ANTHROPIC_MODEL=claude-sonnet-5-5 \
.venv/bin/python -m pytest -m live tests/live
```

### OpenAI-compatible adapter

`assistant/openai_provider.py` is the only module that imports the `openai` SDK, and
`ADAPTERS` imports it only when an `openai_compatible` configuration is built. One adapter
serves OpenAI and compatible servers (gateways and local model servers) through either
protocol. Building it makes no request. Until the protocol mappings land (steps 41.3 and
41.4), it sends nothing: each request fails with `provider_unavailable`.

```toml
kind = "openai_compatible"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"
endpoint = "https://api.openai.com/v1"   # required: the API root, including any /v1
protocol = "responses"                    # or "chat_completions"; never guessed
capabilities = ["tool_calling", "parallel_tool_calls"]
```

Nothing is guessed. `endpoint` and `protocol` are required, and requests go to
`{endpoint}/responses` or `{endpoint}/chat/completions`. A compatible server cannot be
asked what its model supports, so `capabilities` declares it: tool calling by default, and
parallel tool calls only when listed, because some servers reject `parallel_tool_calls`.
`ask` always offers tools, so a configuration that does not declare `tool_calling`, for a
model without function calling, fails with `provider_unsupported_capability` before any
request, and one that allows more than one call per turn without declaring
`parallel_tool_calls` fails the same way.

The client comes only from the configuration, through the same shared guarded clients as
the Anthropic adapter:

- The key from `api_key_env` and the `endpoint` are passed explicitly, so `OPENAI_API_KEY`
  and `OPENAI_BASE_URL` are never used.
- Every request passes through a transport that keeps only the SDK's own headers and then
  sets the configured key as `Authorization: Bearer`, so `OPENAI_ORG_ID`,
  `OPENAI_PROJECT_ID`, `OPENAI_CUSTOM_HEADERS`, and `OPENAI_ADMIN_KEY` cannot add headers
  or replace the credential. Workload identity is never configured, and a client with its
  own transport uses no environment proxy.
- Redirects are not followed, the SDK's own retries are off (`max_retries = 0`), and the
  timeouts are `connect_timeout_seconds` and `request_timeout_seconds`.
- The `openai` and `httpx2` loggers are held at `WARNING` whatever `OPENAI_LOG` or the root
  level says, because their debug records hold request bodies.

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
retry, and timeout cases. The orchestration and answer-service suites and the README example
questions run through every harness too, and check that each adapter reaches the same run
and answer as the fake from the same turns; the Anthropic harness serves them from a mocked
Messages API. An adapter's harness subclasses `MockedHarness`, giving its protocol's response
body and how its API shows each scripted error; the mocked transport, scripted failures, and
the loopback server for installed-command tests are in `tests/support/mock_api.py`. Two orchestration cases are the fake's alone: a response arriving after the
provider's own timeout, which an HTTP client cannot receive, and another provider's
continuation, since an adapter marks its own.
