# Assistant provider contracts

This page describes the provider-neutral layer under `src/archdiscov/assistant/`: the
conversation and tool-call contracts, provider errors and capabilities, the `Provider`
protocol, provider configuration, and the scripted fake provider used to test everything
above it. The `ask` command builds its provider from a configuration file: the fake, the
[Anthropic adapter](#anthropic-adapter), or the
[OpenAI-compatible adapter](#openai-compatible-adapter), which answers through Responses
or Chat Completions, or the [Bedrock adapter](#bedrock-adapter), which is built from its
own AWS profile and answers through Converse. The
[assistant tools](assistant-tools.md), [orchestration](assistant-orchestration.md), and
[answers](assistant-answers.md) build on this layer, and
[evaluation](assistant-evaluation.md) runs the same questions across several configurations.
Only an adapter module imports a vendor SDK, and only when its kind is built.

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
retries a rejected answer; an invalid answer gets at most one
[repair request](assistant-answers.md#outcomes-and-exit-codes) from the answer service.

## Configuration

`assistant/config.py` loads a TOML file of at most 64 KiB (`MAX_CONFIG_BYTES`) with
`load_provider_config(path)`, or validates data with `parse_provider_config`. The file
is one table selected by `kind`. The `ask` command reads it with `--config`;
every kind has an adapter.

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
| `endpoint` | `anthropic`, `bedrock` | none | HTTPS URL |
| `endpoint` | `openai_compatible` | required | HTTPS URL |
| `protocol` | `openai_compatible` | required | `responses`, `chat_completions` |
| `capabilities` | `openai_compatible`, `bedrock` | `tool_calling` | a list of `tool_calling`, `parallel_tool_calls` (which requires `tool_calling`), `structured_output` |
| `encrypted_reasoning` | `openai_compatible` | `false` | `true` or `false`; `true` requires `protocol = "responses"` |
| `profile` | `bedrock` | required | AWS profile name |
| `region` | `bedrock` | required | a concrete AWS region, not `global` |
| `role_arn` | `bedrock` | none | IAM role ARN |
| `session_name` | `bedrock` | `archdiscov-assistant` | non-blank |

Numbers are strictly typed: `"5"` or `true` is not a number. Endpoints may use HTTP only
for loopback hosts (`localhost`, `127.0.0.1`, `::1`), and never carry user info, a query,
or a fragment; a password in an endpoint is refused as a secret-looking value
(`provider_config_secret_value`) before the endpoint is checked. `request_limits()` turns a configuration into the per-request limits
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
- `bedrock` builds the [Bedrock adapter](#bedrock-adapter); a profile that cannot be loaded
  fails with `provider_profile_unavailable`, naming the profile.

A test checks that `ADAPTERS` has an adapter for every kind, so no configuration that
validates can name a kind nothing builds.

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
.venv/bin/python -m pytest -m live tests/live/test_anthropic_live.py
```

Both live tests share `tests/support/live.py`, which writes the capped configuration and
checks the run.

### OpenAI-compatible adapter

`assistant/openai_provider.py` is the only module that imports the `openai` SDK, and
`ADAPTERS` imports it only when an `openai_compatible` configuration is built. One adapter
serves OpenAI and compatible servers (gateways and local model servers) through either
protocol, [Responses](#responses) or [Chat Completions](#chat-completions), each a
request-body and response function behind one table keyed by protocol, so the orchestrator
cannot tell them apart. Building it makes no request. Failures and retries are shared by
both protocols, as described under [OpenAI-compatible failures](#openai-compatible-failures).

```toml
kind = "openai_compatible"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"
endpoint = "https://api.openai.com/v1"   # required: the API root, including any /v1
protocol = "responses"                    # or "chat_completions"; never guessed
capabilities = ["tool_calling", "parallel_tool_calls"]
encrypted_reasoning = true                # responses only: replay a reasoning model's reasoning
```

Nothing is guessed. `endpoint` and `protocol` are required, and requests go to
`{endpoint}/responses` or `{endpoint}/chat/completions`. A compatible server cannot be
asked what its model supports, so `capabilities` declares it: tool calling by default, and
parallel tool calls only when listed, because some servers reject `parallel_tool_calls`.
`ask` always offers tools, so a configuration that does not declare `tool_calling`, for a
model without function calling, fails with `provider_unsupported_capability` before any
request, and one that allows more than one call per turn without declaring
`parallel_tool_calls` fails the same way.

`endpoint` is the API root including any version path, such as
`https://api.openai.com/v1`, a gateway's root, or a local model server's. Every request
carries the key as `Authorization: Bearer`, and no other credential header, so a gateway
that expects its key elsewhere is not supported. Plain HTTP is accepted only for loopback
hosts, such as a local model server or a mock API:

```toml
kind = "openai_compatible"
model = "llama3.1"
api_key_env = "LOCAL_LLM_KEY"     # still required; any non-blank value if the server ignores it
endpoint = "http://localhost:11434/v1"
protocol = "chat_completions"
```

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

#### Chat Completions

With `protocol = "chat_completions"`, each request becomes one `chat.completions.create`
call, holding only fields the protocol defines:

- System and user messages become `system` and `user` messages, in order. An assistant
  message becomes an `assistant` message with its text as `content` (left out when there is
  none) and its calls as `function` tool calls whose arguments are compact JSON text; a call
  with malformed arguments, which never ran, is replayed as `{}`. Each tool result becomes
  its own `tool` message with its `tool_call_id`; the protocol has no error flag, so a
  failed tool's content says so itself.
- Tools become `function` declarations with `name`, `description`, and `parameters`, with
  `tool_choice` `auto`. `parallel_tool_calls` is sent only when `parallel_tool_calls` is
  declared, set to whether `max_tool_calls_per_turn` exceeds 1. `max_completion_tokens` is
  `max_output_tokens`, and the request's timeout is passed with it.
- No sampling parameters, `n`, or streaming are sent; the model's defaults apply. Both
  protocols send the same tool choice and parallel-calls field.

The response is untrusted. It must hold exactly one choice; several or none are
`provider_malformed_output`. The message's `content` is the text, and its `function` tool
calls become tool calls through the shared argument parsing, so invalid JSON and
non-object arguments are malformed-arguments values; any other tool-call type, or a
response that does not fit the contracts, is `provider_malformed_output`. A non-empty
`refusal` field is the `refusal` stop with its text, whatever the finish reason. Usage maps
`prompt_tokens` (which include cached tokens), `completion_tokens`, and
`prompt_tokens_details` `cached_tokens` and `cache_write_tokens` as cache read and write.

| `finish_reason` | Maps to |
|---|---|
| `stop` | `end` |
| `tool_calls` | `tool_use` |
| `length` | `length` for text alone; otherwise `provider_truncated_output` |
| `content_filter` | `provider_refusal` |
| any other, such as `function_call` | `provider_malformed_output` |

Chat Completions returns no reasoning to replay, so it never sets a continuation, and a
conversation holding one, from any provider kind, is refused as `provider_invalid_request`
before anything is sent.

#### Responses

With `protocol = "responses"`, each request becomes one stateless `responses.create` call:
`store` is false and no `previous_response_id` is sent, so the whole conversation goes as
`input` every time.

- System and user messages become `message` input items with role `system` or `user`. An
  assistant message that carries a continuation is replayed as its continuation's output
  items, unchanged and in order; otherwise it becomes an `assistant` message item (when it
  has text) followed by a `function_call` item per call, with its `call_id` and compact
  JSON arguments (`{}` for malformed ones). Each tool result becomes a
  `function_call_output` item with its `call_id`; as in Chat Completions there is no error
  flag.
- Tools become `function` tools with `name`, `description`, `parameters`, and `strict`
  false, since Responses otherwise makes functions strict, which needs schemas the tool
  contracts do not require. `tool_choice` and `parallel_tool_calls` are as for Chat
  Completions, and `max_output_tokens` is `max_output_tokens`.
- `include` holds `reasoning.encrypted_content` only when `encrypted_reasoning` is true.

A stateless reasoning model needs its reasoning back with its function calls, and only the
encrypted content can carry it, since nothing is stored. The official guidance could not
be checked for whether asking for it is safe with models and servers that do not reason,
and compatible servers need not know the field, so it is a declared choice, off by default,
and valid only with `protocol = "responses"` (otherwise
`provider_config_reasoning_protocol`). Turn it on for OpenAI reasoning models.

The response is untrusted. Its `output` items are read in order: `message` items give their
`output_text` parts as text and their `refusal` parts as a refusal, `function_call` items
become tool calls (by `call_id`) through the shared argument parsing, and `reasoning`
items are kept for replay; any other item or part type, such as a server tool call, is
`provider_malformed_output`, as is a response that does not fit the contracts. A refusal
part is the `refusal` stop with its text, whatever the status. Usage maps `input_tokens`
(which include cached tokens), `output_tokens`, and `input_tokens_details` `cached_tokens`
and `cache_write_tokens` as cache read and write.

| `status` | `incomplete_details.reason` | Maps to |
|---|---|---|
| `completed` | none | `end`, or `tool_use` when it holds function calls |
| `incomplete` | `max_output_tokens` | `length` for text alone; otherwise `provider_truncated_output` |
| `incomplete` | `content_filter` | `provider_refusal` |
| any other, such as `failed` | any | `provider_malformed_output` |

A response holding a `reasoning` item with encrypted content keeps its whole output, in
order, as an `openai_compatible` continuation, and the next request replays that output
verbatim, as the Anthropic adapter does with thinking blocks. Reasoning without encrypted
content cannot be replayed to a server that stores nothing, so such a response is rebuilt
from its text and calls without it. A continuation from another provider kind is refused
as `provider_invalid_request` before anything is sent.

#### OpenAI-compatible failures

Failures map as for the Anthropic adapter, by the SDK's exception class through the class
hierarchy and never by message text, with a fixed message and no provider text. The SDK
never raises its own finish-reason errors, because only `create` is called, never its
parsing helpers.

| `openai` exception | Status | Provider error |
|---|---|---|
| `BadRequestError` | 400 | `provider_invalid_request` |
| `NotFoundError` | 404, such as an unknown model | `provider_invalid_request` |
| `ConflictError` | 409 | `provider_invalid_request` |
| `UnprocessableEntityError` | 422 | `provider_invalid_request` |
| `AuthenticationError` | 401 | `provider_authentication` |
| `PermissionDeniedError` | 403 | `provider_authentication` |
| `RateLimitError` | 429 | `provider_rate_limit` |
| `InternalServerError` | any 5xx | `provider_unavailable` |
| `APIStatusError` | any other status, such as 413 or a redirect | `provider_invalid_request` |
| `APITimeoutError` | none: the request timed out | `provider_timeout` |
| `APIConnectionError` | none: the API could not be reached | `provider_transport` |
| `APIResponseValidationError` | a response that does not fit the SDK | `provider_malformed_output` |
| `JSONDecodeError` | a success body that is not JSON | `provider_malformed_output` |
| `APIError` | any other SDK failure | `provider_unavailable` |

Retries, waits, and `retry-after` use the same shared loop and constants as the
[Anthropic adapter](#failures-and-retries): only transport and rate-limit errors are
retried, up to `max_retries`, and a timeout never is.

#### OpenAI-compatible live smoke tests

`tests/live/test_openai_live.py` asks the same README question, with the same caps, once
per protocol. It is marked `live` and skips unless variables name the key variable, the
endpoint, and the model, and list the protocols to run; a protocol the list leaves out
skips with its reason. `ARCHDISCOV_LIVE_OPENAI_ENCRYPTED_REASONING=1` declares
`encrypted_reasoning` for the Responses run. Run it only on purpose:

```sh
ARCHDISCOV_LIVE_OPENAI_KEY_ENV=OPENAI_API_KEY \
ARCHDISCOV_LIVE_OPENAI_ENDPOINT=https://api.openai.com/v1 \
ARCHDISCOV_LIVE_OPENAI_MODEL=gpt-5 \
ARCHDISCOV_LIVE_OPENAI_PROTOCOLS=responses,chat_completions \
.venv/bin/python -m pytest -m live tests/live/test_openai_live.py
```

### Bedrock adapter

`assistant/bedrock_provider.py` is the only assistant module that imports `boto3` or
`botocore`, and `ADAPTERS` imports it only when a `bedrock` configuration is built. Building
it makes no request; each `complete` then makes one Converse request. Botocore is
synchronous, so `complete_async` runs the request in a daemon thread. An interrupt (Ctrl-C)
therefore ends the command at once instead of waiting out the request's read timeout.

```toml
kind = "bedrock"
model = "anthropic.claude-3-5-sonnet-20240620-v1:0"   # a model ID, inference profile ID, or ARN
profile = "assistant"                         # its own profile, not collection's
region = "eu-west-1"
role_arn = "arn:aws:iam::123456789012:role/ArchdiscovAssistant"   # optional
capabilities = ["tool_calling", "parallel_tool_calls"]
```

The adapter has its own AWS session, separate from the credentials collection uses: the
configured `profile` and `region`, and, with `role_arn`, a role assumed as `session_name`.
The session code is shared with collection (`ingestion/aws_sessions.py`), but no
credential or setting is. A role is assumed on the first request, through refreshable
credentials, so building makes no request and an STS failure is that request's failure.
A profile that does not exist, or whose files cannot be read, fails before the run with
`provider_profile_unavailable` (exit 2), naming the profile and never its contents.

Only the configuration chooses the target, the credential, and the retries:

- An explicit profile makes botocore skip `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, and `AWS_PROFILE`; the region is always the configured one.
- `AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`, and a profile's `endpoint_url` are
  ignored, as are FIPS and dual-stack settings. Requests go to the regional
  `bedrock-runtime` endpoint, or to `endpoint` when set, such as a VPC interface endpoint;
  plain HTTP is accepted only for loopback hosts.
- Proxy variables are ignored, because the client is given an explicit empty proxy
  setting.
- Botocore's own retries are off whatever `AWS_MAX_ATTEMPTS`, `AWS_RETRY_MODE`, or the
  profile say, and the timeouts are `connect_timeout_seconds` and
  `request_timeout_seconds`.
- The `boto3` and `botocore` loggers are held at `WARNING` whatever the root level says,
  because their debug records hold request bodies, and `botocore.credentials` at `ERROR`
  (see [Bedrock failures](#bedrock-failures)).

`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE` still choose where profiles are read
from, as for any AWS tool.

The identity the adapter signs with (the profile's, or the role's when `role_arn` is set)
needs these IAM permissions, and nothing else:

- `bedrock:InvokeModel` on the model: the foundation model's ARN for a model ID, or the
  inference profile's ARN for an inference profile. A cross-Region inference profile also
  needs `bedrock:InvokeModel` on the underlying foundation model in every Region the profile
  routes to. Converse uses this permission; the model must also be enabled for the account.
- `sts:AssumeRole` on `role_arn`, for the profile's identity, when a role is set; the role's
  trust policy must allow that identity.

For example, for an inference profile:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-sonnet-5-5-v1:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5-5-v1:0"
      ]
    }
  ]
}
```

An access-denied answer is `provider_authentication` (exit 6), with no ARN or message.

Bedrock models differ in tool support and cannot be asked cheaply, so `capabilities`
declares it, as for the OpenAI-compatible adapter: tool calling by default, parallel tool
calls only when listed. Converse has no setting that stops a model from calling several
tools in one turn, so a model that does so with `max_tool_calls_per_turn` at 1 ends the
run as `limit_reached`; declare `parallel_tool_calls` and raise the limit for such models.

#### Converse requests and responses

Each request becomes one `converse` call holding only fields every tool-capable model
accepts:

- System messages become `system` text blocks. User, assistant, and tool-result messages
  become `messages` in order; Converse requires alternating roles, so consecutive
  user-role messages, such as tool results and a repair prompt, share one `user` message.
- An assistant message that carries a continuation is replayed as its continuation's
  content, unchanged and in order; otherwise it becomes a `text` block (when it has text)
  followed by a `toolUse` block per call, with its `toolUseId` and JSON object `input`
  (`{}` for malformed arguments, which never ran).
- Each tool result becomes a `toolResult` block with one `text` content block, and a failed
  tool's result has `status` `error`.
- Tools become `toolSpec` entries with `name`, `description`, and `inputSchema.json`, and
  `inferenceConfig.maxTokens` is `max_output_tokens`.
- `toolChoice` is not sent, and a successful result has no `status`: both are the
  defaults (`auto` and success), and some models reject the fields. Nothing goes in
  `additionalModelRequestFields`, `guardrailConfig`, `performanceConfig`, or the sampling
  fields, so the model's defaults apply.

The response is untrusted: botocore parses it into plain dictionaries without checking
it. Only `text`, `toolUse`, and `reasoningContent` blocks are mapped. Any other block is
`provider_malformed_output`, as is a response that does not fit the contracts. That
includes images, documents, citations, guard content, a tool result, a server tool's
`toolUse` (one with a `type`), and reasoning of a kind botocore does not know, which it
parses without its content and so could not go back. Text blocks are joined, and
`toolUse` blocks become tool calls through the shared argument parsing. Usage maps
`inputTokens`, `outputTokens`, `cacheReadInputTokens`, and `cacheWriteInputTokens`.

| `stopReason` | Maps to |
|---|---|
| `end_turn`, `stop_sequence` | `end` |
| `tool_use` | `tool_use` |
| `max_tokens`, `model_context_window_exceeded` | `length` for text alone; otherwise `provider_truncated_output` |
| `guardrail_intervened`, `content_filtered` | `refusal` for text alone; otherwise `provider_refusal` |
| any other, such as `malformed_model_output` or `malformed_tool_use` | `provider_malformed_output` |

Models that reason, such as Anthropic's on Bedrock, need their reasoning back with tool
use. A response holding `reasoningContent` blocks therefore keeps its whole content, in
order, as a `bedrock` continuation, and the next request replays that content verbatim.
Redacted reasoning is kept as the base64 text the API sent, so it goes back byte for byte.
A continuation from another provider kind is refused as `provider_invalid_request` before
anything is sent.

Botocore fixes the read timeout when it makes a client, but each request carries its own
`timeout_seconds`, which the run's deadline can cut short. A request whose timeout differs
from `request_timeout_seconds` is therefore sent by a client made for that timeout, from the
same session, so a role is still assumed once. The configured timeout's client is kept, and
only the latest other one.

#### Bedrock failures

Botocore raises one `ClientError` for every API error, so an API error maps by its error
code, never by message text; any other code, or none, is `provider_unavailable`. Every
error the Converse operation model declares has a row, which a test checks.

| API error code | Provider error |
|---|---|
| `ThrottlingException` | `provider_rate_limit` |
| `ServiceQuotaExceededException` | `provider_rate_limit` |
| `AccessDeniedException` | `provider_authentication` |
| `UnrecognizedClientException` | `provider_authentication` |
| `ExpiredTokenException` | `provider_authentication` |
| `InvalidSignatureException` | `provider_authentication` |
| `IncompleteSignature` | `provider_authentication` |
| `MissingAuthenticationTokenException` | `provider_authentication` |
| `InvalidClientTokenId` | `provider_authentication` |
| `RequestExpired` | `provider_authentication` |
| `ValidationException` | `provider_invalid_request`, including a model without tool support |
| `ResourceNotFoundException` | `provider_invalid_request` |
| `ModelTimeoutException` | `provider_timeout` |
| `ModelNotReadyException` | `provider_unavailable` |
| `ModelErrorException` | `provider_unavailable` |
| `InternalServerException` | `provider_unavailable` |
| `ServiceUnavailableException` | `provider_unavailable` |

Botocore's own failures map by class, through the class hierarchy, so a subclass maps
before its base:

| botocore exception | Cause | Provider error |
|---|---|---|
| `ClientError` | an API error | by its code, above |
| `ReadTimeoutError` | the response did not arrive within the request's timeout | `provider_timeout` |
| `ConnectTimeoutError` | no connection within `connect_timeout_seconds` | `provider_timeout` |
| `HTTPClientError` | the connection failed during the request | `provider_transport` |
| `ConnectionError` | the endpoint, a proxy, or TLS could not be reached | `provider_transport` |
| `NoCredentialsError` | the profile has no credentials | `provider_authentication` |
| `PartialCredentialsError` | the profile's credentials are incomplete | `provider_authentication` |
| `CredentialRetrievalError` | the role could not be assumed | `provider_authentication` |
| `TokenRetrievalError` | a login token could not be refreshed | `provider_authentication` |
| `SSOError` | the profile's SSO session is missing or expired | `provider_authentication` |
| `ParamValidationError` | the request does not fit the service model | `provider_invalid_request` |
| `ResponseParserError` | a response botocore cannot parse | `provider_malformed_output` |
| `BotoCoreError` | any other botocore failure | `provider_unavailable` |
| `AttributeError` | a response field of the wrong type, as botocore parses it | `provider_malformed_output` |
| `TypeError` | likewise | `provider_malformed_output` |
| `ValueError` | likewise, such as a non-numeric token count | `provider_malformed_output` |

A role is assumed on the first request, so a role assumption that STS rejects, or that
answers without usable credentials, is that request's `CredentialRetrievalError`, and so
`provider_authentication`; an STS timeout or connection failure maps as for Converse.
Botocore warns of a failed role assumption with a traceback that holds STS's message,
naming the role, so the `botocore.credentials` logger is held at `ERROR`. Botocore parses a
success body that is not JSON as an empty response, which the mapping refuses as
`provider_malformed_output`. As for the other adapters, an error has its fixed message and
chains the botocore exception, copying none of its message, request ID, or ARN.

Retries, waits, and `retry-after` use the same shared loop and constants as the
[Anthropic adapter](#failures-and-retries): only transport and rate-limit errors,
throttling included, are retried, up to `max_retries`, and a timeout never is. Botocore's
own retries stay off.

#### Bedrock live smoke test

`tests/live/test_bedrock_live.py` asks the same README question, with the same caps. It is
marked `live` and skips unless variables name the AWS profile, region, and a model that
supports tool use; another optionally names a role to assume. The profile's secret key and
session token must not reach the result or the transcript. Run it only on purpose:

```sh
ARCHDISCOV_LIVE_BEDROCK_PROFILE=assistant \
ARCHDISCOV_LIVE_BEDROCK_REGION=eu-west-1 \
ARCHDISCOV_LIVE_BEDROCK_MODEL=eu.anthropic.claude-sonnet-5-5-v1:0 \
.venv/bin/python -m pytest -m live tests/live/test_bedrock_live.py
```

### Credentials

- Credentials are references only: `api_key_env` names an environment variable, and
  Bedrock uses its own AWS `profile` (and optional role), separate from collection
  credentials.
- API keys are never accepted as CLI arguments, which leak into shell history and
  process listings, or as configuration values.
- Before field validation, the whole file is scanned: credential-like field names
  (such as `api_key`, `password`, `token`, or `authorization`, at any depth) and
  secret-looking values (API-key prefixes, AWS access key IDs and secret access keys,
  bearer tokens, JSON web tokens, Slack tokens, private keys, passwords in URLs and
  connection strings, and long mixed-case opaque tokens) are rejected.
- `resolve_api_key(config)` reads the variable only when a provider is constructed, and
  fails with `provider_credential_missing` or `provider_credential_empty` before any
  request. It returns a `SecretStr`, which `repr`, `str`, and JSON dumps mask;
  configurations never hold resolved values.

`ProviderConfigError` carries a code (`provider_config_unreadable`,
`provider_config_too_large`, `provider_config_malformed`, `provider_config_invalid`,
`provider_credential_missing`, `provider_credential_empty`,
`provider_transcript_missing`, or `provider_profile_unavailable`), a fixed message, the
file path, the referenced variable or AWS profile name, and code-only issue details,
never file contents or field values.

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
`tests/support/provider_harness.py` through the same retry and timeout cases, and each
adapter's harness through the same mapping and error cases too (the fake returns and raises
what its transcript scripts, which its own tests cover). It is the one place that tests what
the adapters share: the retry policy, the guarded HTTP clients (no redirects, no target or
credential from the environment), quiet SDK loggers, and an error for every SDK failure.
Capability checks belong to the shared entry points and are tested once, in
`tests/assistant/test_provider.py`. Each adapter's own tests cover only its request and
response mapping, usage, continuations, and, for Bedrock, its
profile, role, and per-timeout clients. The orchestration and answer-service suites and the README example
questions run through every harness too, and check that each adapter reaches the same run
and answer as the fake from the same turns, so their per-outcome checks run on the fake alone;
each adapter's harness serves them from its own mocked API. An adapter's harness subclasses `MockedHarness`, giving its protocol's response
body and how its API shows each scripted error; the Bedrock harness also says how its API
serves a body and is made, since botocore is mocked through its `before-send` event (with
`BotocoreApi`) rather than an HTTP transport, and reads a dummy AWS profile from
`tests/fixtures/assistant/aws/`. The mocked transports, scripted failures, and the loopback
server for installed-command tests are in `tests/support/mock_api.py`. Two orchestration
cases are the fake's alone: a response arriving after the provider's own timeout, which an
HTTP client cannot receive, and another provider's continuation, since an adapter marks its
own.
