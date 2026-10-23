# Assistant evaluation

Evaluation runs the same questions across configured providers and scores each answer
against deterministic fixtures: the findings the analyzers return, never the wording of an
answer. This page describes the suite and report contracts and the scoring in
`assistant/evaluation.py`, and running a suite with `services/evaluation.py`. Saving and
comparing reports, and the commands, arrive in later steps of the plan (43.4–43.5).

## Suites

A suite is a versioned JSON file of cases. Each case is one question over the files an
[`ask`](ask-command.md) run selects, and what its answer must show:

```json
{
  "schema_version": "1",
  "name": "readme",
  "cases": [
    {
      "id": "ownership",
      "question": "Which resources appear to have no clear owner?",
      "snapshot": "ownership/snapshot.json",
      "fake_transcript": "ownership/transcript.json",
      "expect": {
        "outcomes": ["answered"],
        "statuses": ["supported"],
        "analyzers": ["ownership"],
        "finding_ids": ["1e0b…"]
      }
    }
  ]
}
```

| Case field | Required | Meaning |
|---|---|---|
| `id` | yes | unique within the suite: lowercase letters, digits, `-`, and `_`, up to 64 characters |
| `question` | yes | the question, as `ask` takes it |
| `snapshot` | yes | the primary snapshot |
| `comparison` | no | a comparison snapshot, as `--compare` |
| `comparison_results` | no | saved comparisons of the two snapshots, as `--comparison-result`; they need `comparison` |
| `fake_transcript` | no | the transcript that scripts the fake provider; other providers ignore it |
| `expect` | yes | what the answer must show, below |

Paths are relative to the suite file, use `/`, and stay inside its directory: absolute
paths, `.` and `..` segments, empty segments, and backslashes are invalid. A suite holds
1–200 cases.

| Expectation | Meaning |
|---|---|
| `outcomes` | required: the outcomes the run may end with, `answered`, `answered_after_repair`, or a [failure reason](assistant-answers.md#outcomes-and-exit-codes) |
| `statuses` | the analysis statuses the answer may have |
| `rejections` | [rejection codes](assistant-answers.md#validation) a rejected answer must include |
| `analyzers` | analyzer kinds the run must run with `run_analyzer` |
| `finding_ids` | finding IDs the answer must cite |
| `limitations` | limitation codes the cited findings must carry |

An empty or missing expectation expects nothing. `statuses`, `finding_ids`, and
`limitations` describe an answer, so `outcomes` must allow one, and `rejections` need
`rejected_after_repair`. Lists hold no duplicates, and unknown fields are invalid.

## Running a suite

`prepare_evaluation(suite_path, config_paths, allow_external=False)` does everything that
can fail before a request, in order, so an error costs nothing and reads no further file:

1. It reads the suite: at most 1 MiB of strict JSON, without duplicate keys or `NaN`-like
   constants, validated as above.
2. It loads each of the 1–10 provider configurations. A kind other than `fake` is refused
   unless external providers are allowed, and is built as `ask` builds it, which resolves
   its credential and makes no request.
3. For a fake configuration, it loads every case's `fake_transcript`, which replaces any
   transcript the configuration names, so each case scripts its own run.
4. It loads and verifies every case's snapshots and saved comparisons, as `ask` does.

The prepared evaluation's `budgets` state each provider's worst case before anything runs.
`run_evaluation(prepared)` then runs every case once per provider, in suite order, through
the same answer path as `ask`, each run with a fresh record over the verified data, and
scores it. Whatever happens during a run, such as a provider error, a limit, or a rejected
answer, is that case's record, and the evaluation goes on. A cancellation, such as Ctrl-C,
ends it: `EvaluationCancelled` carries the report so far, with the interrupted case recorded
as `cancelled` and that provider's run, and every later one, marked cancelled.
`evaluate_suite` prepares and runs in one call, and every function takes a `clock` for
latency and a `now` for the report's creation time, so tests can fix both.

Errors before the run are those `ask` raises for configurations, credentials, fake
transcripts, snapshots, and saved results, plus:

| Code | When |
|---|---|
| `evaluation_suite_unreadable` | the suite file cannot be read |
| `evaluation_suite_too_large` | the suite file exceeds 1 MiB |
| `invalid_evaluation_suite` | the suite is not strict JSON or does not validate |
| `evaluation_providers_invalid` | there are no configurations, or more than 10 |
| `evaluation_external_not_allowed` | a configuration of a kind other than `fake` without the opt-in |
| `evaluation_transcript_missing` | a fake configuration, and a case without a `fake_transcript` |

The suite errors carry the suite's path, a refused external configuration its path, and a
missing transcript the case's ID; none carries a value from the file.

## Scoring

`score_case(case, result)` turns one `ask` result into a case record. It reads only what the
answer service validated and rendered: the outcome, the analysis status, the IDs the
conclusions cite, the limitation codes of the cited findings, and the analyzer kinds of the
run's successful `run_analyzer` calls. Each expectation the case sets becomes a check. A
check that describes an answer is left out when the run did not answer, since the outcome
check already fails or allows that.

Three safety checks apply to every case, whatever its expectations, and a case passes only
if every check does:

| Check | Passes when |
|---|---|
| `cited` | every conclusion has citations and cites each finding it shows |
| `qualified` | a `supported` answer rests only on supported findings and no recorded differences, and any weaker status shows qualifications |
| `quoted` | no imported text the answer quotes (quoted data, finding summaries, limitation messages) of at least 16 characters appears, ignoring case and spacing, in its statements, qualifications, failure explanation, or narrative |

`citations_valid` is true for an answer, false for an answer rejected for an ID it may not
cite (`answer_side_unavailable`, `answer_unknown_id`, `answer_other_snapshot_id`,
`answer_id_not_returned`, or `answer_evidence_detached`), true for an answer rejected for
other reasons, and null when the run ended before any proposal was validated.

## Reports

A report (schema version `"1"`) names its suite by name, digest, and case IDs, and holds,
for each provider configuration, its kind, model, run limits, budget, one record per case
in suite order, whether the run was cancelled, and totals.

| Record field | Meaning |
|---|---|
| `id` | SHA-256 of the record's content, without its ID or latency |
| `case_id` | the case |
| `outcome` | the [answer outcome](assistant-answers.md#outcomes-and-exit-codes): status, failure reason, rejections, provider error, or limit |
| `analysis_status` | the answer's analysis status, or null |
| `cited_finding_ids`, `cited_evidence_ids`, `cited_resource_ids` | what the conclusions cite, sorted |
| `limitation_codes` | the cited findings' limitation codes, sorted |
| `citations_valid` | as above |
| `analyzers_run` | the analyzer kinds the run ran, sorted |
| `usage` | provider requests, tool calls by name, whether a repair was requested, and token usage |
| `latency_seconds` | the run's latency |
| `checks` | each check and whether it passed, in a fixed order |
| `passed` | whether every check passed |

The suite digest is the SHA-256 of the suite's canonical JSON. A record's ID leaves out
latency, so a deterministic run, such as one with the fake provider, gives the same record
IDs every time. A provider's budget states the most its runs may use: one run per case, each
with up to `max_provider_requests` requests plus the repair, each of up to
`max_output_tokens` output tokens. Totals sum the records' cases, passes, failures,
requests, repairs, tool calls, tokens, and latency.

A report holds codes, IDs, counts, and scores only: never a question's narrative, quoted
data, tool output, provider text, configuration values, or credentials, and a model name
that looks like a secret is invalid.

## Validation

`parse_suite` and `parse_report` validate data and fail with `EvaluationValidationError`,
whose code is `invalid_evaluation_suite` or `invalid_evaluation_report`; `load_suite` reads a
suite file with the codes above. Its details give
only known field names, list positions, and fixed messages, never the input. A report is
also invalid when a record's ID does not match its content, its lists are not sorted and
unique, its checks are out of order or lack a safety check, `passed` disagrees with its
checks, totals or the budget disagree with the records and limits, or the records do not
follow the suite's cases (all of them, unless the run was cancelled).
