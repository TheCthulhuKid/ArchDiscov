# The ask command

`archdiscov ask` answers one natural-language question about selected snapshots with a
configured provider. The model reads the snapshots only through the bounded
[assistant tools](assistant-tools.md) in a [tool-calling loop](assistant-orchestration.md),
then submits a structured proposal; the answer shown is
[validated and rendered from records](assistant-answers.md), and a failed run is explained
instead. Each run is one question: there is no chat or conversation memory.

The only provider that answers is the scripted fake, which replays a transcript and needs no
credentials or network. The [Anthropic adapter](assistant-providers.md#anthropic-adapter) is
being added: an `anthropic` configuration builds, but its first request fails as
`provider_error` (6) without contacting the API. The other real kinds are accepted in
configuration but fail before the run with `provider_adapter_unavailable`.

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

A real kind names its credential by environment variable (`api_key_env`) or AWS profile,
never by value, and the key is never accepted as an option. A missing or empty key fails
with `provider_credential_missing` or `provider_credential_empty` before any snapshot is
loaded. A kind without an adapter fails with `provider_adapter_unavailable` before its
credential is read.

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
| 2 | invalid input before the run: `invalid_question`, `provider_config_too_large`, `provider_config_malformed`, `provider_config_invalid`, `provider_credential_missing`, `provider_credential_empty`, `provider_transcript_missing`, `provider_adapter_unavailable`, `fake_transcript_too_large`, `fake_transcript_invalid`, `invalid_snapshot`, `invalid_comparison_result`, `result_snapshot_unselected`, a fake scripting error, or a usage error |
| 3 | the `--transcript` path exists (`snapshot_conflict`) |
| 5 | no valid answer: `rejected_after_repair`, `no_proposal_after_repair`, or `malformed_output` |
| 6 | the run stopped: `provider_error` or `limit_reached`; a script may retry |
| 130 | the run was cancelled (`cancelled`), for example with Ctrl-C |

Codes 0, 5, 6, and 130 are [answer outcomes](assistant-answers.md#outcomes-and-exit-codes):
the run started and its answer or failure explanation is printed. Codes 1, 2, and 3 are
diagnostics before the run, on standard error, or as JSON on standard output with `--json`.
Diagnostics name the failing file and the provider kind, never configuration values,
credentials, or the question.

## Verification

`tests/acceptance/test_installed_ask.py` runs the installed command outside the checkout,
through the `archdiscov` script and `python -m archdiscov`, with an offline guard that fails
any network use and an environment without credentials. It answers the five README example
questions in both output forms, refuses a missing configuration, an unknown snapshot, a real
provider kind without an adapter, and an existing transcript path, and reports a scripted
provider error and a run limit, each with the exit code above. In-process tests in
`tests/cli/test_ask_cli.py` and `tests/cli/test_ask_examples.py` cover every exit code,
the envelope, and further examples (limited coverage, insufficient evidence, a Terraform
plan, and a comparison).
