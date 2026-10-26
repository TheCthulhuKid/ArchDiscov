# The ask command

`archdiscov ask` answers one natural-language question about selected snapshots with a
configured provider. The model reads the snapshots only through the bounded
[assistant tools](assistant-tools.md) in a [tool-calling loop](assistant-orchestration.md),
then submits a structured proposal; the answer shown is
[validated and rendered from records](assistant-answers.md), and a failed run is explained
instead. Each run is one question: there is no chat or conversation memory. To run many
questions across providers and score the answers, see [evaluation](assistant-evaluation.md).
To let an MCP client such as Claude Code answer with the same tools and validation, see the
[MCP server](mcp-server.md).

The scripted fake replays a transcript and needs no credentials or network. An
`anthropic` configuration answers through the
[Anthropic adapter](assistant-providers.md#anthropic-adapter) with the key its
`api_key_env` names; an API or connection error ends the run as `provider_error` (6) with
the [provider error](assistant-providers.md#failures-and-retries) it maps to, after any
retries. An `openai_compatible` configuration builds the
[OpenAI-compatible adapter](assistant-providers.md#openai-compatible-adapter), which
answers through either `protocol`; an API or connection error ends the run as
`provider_error` (6) with the
[provider error](assistant-providers.md#openai-compatible-failures) it maps to, after any
retries. Either protocol ends with `provider_unsupported_capability` before any request when the
configuration does not declare tool calling. A `bedrock` configuration builds the
[Bedrock adapter](assistant-providers.md#bedrock-adapter) from its own AWS profile, which
answers through Converse; an API, connection, or credential failure ends the run as
`provider_error` (6) with the [provider error](assistant-providers.md#bedrock-failures) it
maps to, after any retries. A profile that cannot be loaded fails before the run with
`provider_profile_unavailable`.

## Usage

```sh
archdiscov ask "Which resources appear to have no clear owner?" \
  --snapshot snapshot.json --config provider.toml
archdiscov ask "What receives traffic from the public internet?" \
  --snapshot snapshot.json --config provider.toml --transcript run.json --json
archdiscov compare --left earlier.json --right current.json --output comparison.json
archdiscov ask "What changed in ownership between the two snapshots?" \
  --snapshot earlier.json --compare current.json --comparison-result comparison.json \
  --config provider.toml
```

`python -m archdiscov ask` takes the same arguments.

| Option | Required | Holds |
|---|---|---|
| `QUESTION` | yes | the question, non-blank and at most 200,000 characters (`MAX_TEXT_CHARS`) |
| `--snapshot` | yes | the primary snapshot JSON |
| `--config` | yes | the provider configuration TOML; one provider per file |
| `--compare` | no | a second snapshot JSON, the comparison side |
| `--comparison-result` | no | a saved comparison of the selected snapshots for the model to read; repeatable |
| `--transcript` | no | a new path for the run transcript; never overwritten |
| `--json` | no | print the JSON envelope, and diagnostics as JSON |

The provider comes only from `--config`; there is no `--provider` option and nothing falls
back to the fake provider. Recorded differences need a saved comparison of the selected
snapshots, made with `compare` and given with `--comparison-result`, because no tool
compares snapshots itself; a saved comparison that names an unselected snapshot is refused.

## Configuration

The configuration file is described under
[provider configuration](assistant-providers.md#configuration): one table selected by
`kind`, with the request and run limits. A fake configuration names its
[transcript](assistant-providers.md#fake-provider), relative to the configuration file or
absolute:

```toml
kind = "fake"
transcript = "transcript.json"
max_requests = 8
```

An Anthropic configuration names the variable that holds its key, and optionally a
gateway's API root as `endpoint`; the
[Anthropic adapter](assistant-providers.md#anthropic-adapter) describes the rest:

```toml
kind = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
max_requests = 6
```

An OpenAI-compatible configuration names its key variable, the API root as `endpoint`, the
`protocol`, and what the model can do as `capabilities`, since a compatible server cannot be
asked; the
[OpenAI-compatible adapter](assistant-providers.md#openai-compatible-adapter) describes the
rest, including `encrypted_reasoning` for reasoning models:

```toml
kind = "openai_compatible"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"
endpoint = "https://api.openai.com/v1"
protocol = "responses"
capabilities = ["tool_calling", "parallel_tool_calls"]
max_tool_calls_per_turn = 4
```

A real kind names its credential by environment variable (`api_key_env`) or AWS profile,
never by value, and the key is never accepted as an option. A missing or empty key fails
with `provider_credential_missing` or `provider_credential_empty` before any snapshot is
loaded. A Bedrock profile that cannot be loaded fails with `provider_profile_unavailable`,
naming the profile, before any snapshot is loaded.

## Readable output

The run summary comes first: the snapshot, comparison, and saved comparison paths, the
provider kind and model, the limits, the provider requests and whether the repair was
requested, the tool calls by name, the reported tokens, and the transcript path. The
answer follows as described under [renderings](assistant-answers.md#renderings):

```text
Snapshot: snapshot.json
Provider: fake, model fake
Limits: 8 provider requests, 32 tool calls (1 per turn), 4096 output tokens per request, 200000 conversation characters, 60s per request, 300s overall
Provider requests: 4
Tool calls: list_analyzers 1, run_analyzer 1, search_resources 1
Tokens: not reported

Answer: answered, exit code 0
Snapshot (primary): 00000000-0000-4000-8000-000000000023
  Source: mock, observed state
  Collected: 2026-10-13T08:00:00+00:00 to 2026-10-13T08:01:00+00:00
Status: supported
Conclusions:
  1. analyzer_finding [supported]
     Finding 1 on the primary snapshot: the ownership analyzer reports a supported finding; its summary is quoted as [1].
     ...
```

Text from snapshots and the model is sanitised and redacted as in the answer renderings.

## JSON envelope

With `--json`, the output is a versioned envelope:

| Envelope key | Holds |
|---|---|
| `schema_version` | `1`; changes whenever the envelope's own shape does |
| `snapshot_path` | the `--snapshot` path |
| `comparison_path` | the `--compare` path, or `null` |
| `comparison_result_paths` | the `--comparison-result` paths, in order; `[]` when none |
| `config_path` | the `--config` path |
| `transcript_path` | the `--transcript` path, or `null` |
| `summary` | the `RunSummary`: `snapshots`, `provider_kind`, `model`, `limits`, and `usage` |
| `result` | the [answer JSON](assistant-answers.md#renderings) (`answer_data`), with its own `schema_version` and `exit_code` |

`usage` holds `provider_requests`, `tool_calls` (a count per tool name), `repair_requested`,
and `tokens`. Errors before the run starts print `{"error": {...}}` instead, with `code`,
`message`, and, where one applies, the `path` of the failing file.

## Transcripts

With `--transcript PATH`, every run that starts publishes its
[run transcript](assistant-orchestration.md#transcript) as JSON at `PATH`, failed and
cancelled runs included: the provider kind and model, the limits, each request with its tool
calls and results, usage, and the outcome, already redacted and bounded. The path is checked
before the run starts, so a conflict costs no provider request: an existing path, including
a dangling symlink, is `snapshot_conflict` (3), and a missing parent directory is
`snapshot_io_error` (1). It is written atomically and never replaces a file. A run that does
not start writes no transcript.

A fake transcript that runs out of turns, misses an `expect`, or returns more than a request
allows is a scripting error, not a provider outcome: the command stops with its `fake_*`
diagnostic (2) and writes no transcript.

## Exit codes

| Exit code | When |
|---|---|
| 0 | the question was answered, first time or after the one repair (`answered`, `answered_after_repair`) |
| 1 | a file could not be read before the run: `provider_config_unreadable`, `fake_transcript_unreadable`, or `snapshot_io_error` for a snapshot, a saved comparison, or the transcript's missing parent directory |
| 2 | invalid input before the run: `invalid_question`, `provider_config_too_large`, `provider_config_malformed`, `provider_config_invalid`, `provider_credential_missing`, `provider_credential_empty`, `provider_transcript_missing`, `provider_profile_unavailable`, `fake_transcript_too_large`, `fake_transcript_invalid`, `invalid_snapshot`, `invalid_comparison_result`, `result_snapshot_unselected`, a fake scripting error, or a usage error |
| 3 | the `--transcript` path exists (`snapshot_conflict`) |
| 5 | no valid answer: `rejected_after_repair`, `no_proposal_after_repair`, or `malformed_output` |
| 6 | the run stopped: `provider_error` or `limit_reached`; a script may retry |
| 130 | the run was cancelled (`cancelled`), for example with Ctrl-C |

Codes 0, 5, 6, and 130 are [answer outcomes](assistant-answers.md#outcomes-and-exit-codes):
the run started and its answer or failure explanation is printed. Codes 1, 2, and 3 are
diagnostics before the run, on standard error, or as JSON on standard output with `--json`.
Diagnostics name the failing file and the provider kind, never configuration values,
credentials, or the question.

## What is not supported

`ask` explains recorded configuration; it does not act on it or go beyond it:

- **Conversation memory.** Each run is one question with no chat history; a follow-up is
  a new run that starts from the selected files again.
- **Remediation.** The tools are read-only: the model cannot change cloud resources,
  snapshots, or saved results, and receives no cloud credentials, shell, or code execution.
- **Effective access.** Findings describe recorded policies, trust statements, and network
  rules. They do not evaluate effective IAM permissions, organization policies, or
  authentication, so an answer never claims that a principal can, or cannot, act.
- **Reachability proof.** Network findings say what recorded configuration allows, denies,
  or leaves unknown. They are not a live connectivity test, and an answer never claims that
  traffic does, or does not, arrive.
- **Facts from the model.** Conclusions are rendered only from findings, evidence, and
  resources the tools returned; the model's own text is shown as a labelled, unverified
  explanation, and a proposal that cites anything else is rejected.
- **Live data.** Answers cover only the selected snapshots, at their collection times; a
  planned or declared snapshot is never presented as deployed state.

## Verification

[Testing the CLI locally](local-cli-testing.md#ask-a-question-with-the-fake-provider) shows
how to try the command by hand with a written fake transcript or the example helper.

`tests/acceptance/test_installed_ask.py` runs the installed command outside the checkout,
through the `archdiscov` script, with an offline guard that fails
any network use and an environment without credentials. It ingests the README evaluation
suite's case files and answers each case, including the five README example questions, in
both output forms, inspects the cited evidence, and checks that a fabricated citation is
rejected after the one repair. It refuses a missing configuration, an unknown snapshot, a
Bedrock profile that cannot be loaded, an Anthropic or OpenAI-compatible configuration
without its key, and an existing transcript path, and reports a scripted provider error and a run
limit, each with the exit code above. It also answers a README question through the
Anthropic adapter, the OpenAI-compatible adapter with each protocol, and the Bedrock adapter,
each against a mock API on one loopback port, the only connection the guard allows, with the
same answer as the fake provider. The Bedrock run alone gets a dummy AWS profile through
`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE`, and its requests are SigV4-signed
`POST /model/{modelId}/converse` calls. In-process tests in
`tests/cli/test_ask_cli.py` and `tests/cli/test_ask_examples.py` cover every exit code,
the envelope, and further examples (limited coverage, insufficient evidence, a Terraform
plan, and a comparison).
