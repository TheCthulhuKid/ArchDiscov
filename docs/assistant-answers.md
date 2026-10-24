# Assistant answers

This page describes the structured answer a model submits at the end of an
[orchestrated run](assistant-orchestration.md), how it is validated against what the
[tools](assistant-tools.md) returned, how its conclusions are written from records, and how
a failed run is explained, the answer service that repairs once and composes them, and the
readable and JSON renderings. The [`ask` command](ask-command.md) presents them with the scripted fake or the
Anthropic adapter, the OpenAI-compatible adapter through Responses or Chat Completions, or
the Bedrock adapter through Converse. [Evaluation](assistant-evaluation.md) scores these
answers against fixtures.

## Boundary

Validation checks citations and consistency, not truth. A proposal that passes cites only
records the run delivered, for the snapshots the user selected, and claims no more than
those records support. It does not show that a conclusion is correct: the cited findings
come from deterministic analyzers, and the facts an answer shows are rendered from those
records, not from the model's words.

Narrative is allowed only in two bounded fields, `summary` and `explanation`, which are
shown as explanation and never as facts. A proposal states which recorded items support
which kind of claim; it never states the facts itself.

## The proposal

The model ends a run by calling `submit_answer` exactly once, on its own; its input is an
`AnswerProposal` (`assistant/answers.py`). The call is never dispatched as a tool. A
malformed or invalid call, or one mixed with other calls, ends the run with
`malformed_output`; a plain-text answer is not a proposal.

| Field | Holds |
|---|---|
| `status` | `supported`, `partial`, or `insufficient_evidence` |
| `contexts` | the source kind of each side's snapshot; the `primary` side is required, `comparison` is the second snapshot when one is selected |
| `conclusions` | each a kind of claim and the citations it rests on |
| `limitations` | acknowledged limitations: the side, a cited finding's ID, and one of its limitation codes |
| `insufficient_reason` | `no_applicable_findings`, `limited_coverage`, `unsupported_analysis`, or `out_of_scope`; required for `insufficient_evidence`, and only then |
| `summary`, `explanation` | narrative, shown only as explanation |

A citation names a side (`primary` or `comparison`) and finding, evidence, and resource IDs;
the session decides which snapshot each side is, so a model never handles snapshot IDs.

| Conclusion kind | Cites |
|---|---|
| `analyzer_finding` | finding IDs, on one side |
| `recorded_configuration` | evidence IDs, on one side, with the resources they belong to |
| `recorded_difference` | recorded items on both sides of a comparison |

Every list and string is bounded, unknown fields are rejected, and duplicate IDs,
citations, sides, conclusions, contexts, and acknowledgements are rejected. `supported` and
`partial` proposals need conclusions.

| Bound | Value |
|---|---|
| `MAX_CONCLUSIONS` | 20 |
| `MAX_CITED_IDS` | 50 |
| `MAX_LIMITATIONS` | 100 |
| `MAX_SUMMARY_CHARS` | 500 |
| `MAX_EXPLANATION_CHARS` | 2,000 |
| `MIN_REPEATED_TEXT_CHARS` | 24 |
| `MIN_NAMED_ID_CHARS` | 12 |

Version `2` of the system instructions asks for this: one `submit_answer` call that cites
only returned IDs, acknowledges every limitation of each cited finding, and sets contexts
from the source kinds the tools reported.

## Validation

`validate_proposal(proposal, session)` (`assistant/answer_validation.py`) returns every
failure, ordered by its location in the proposal (such as
`conclusions[1].citations[0].finding_ids[0]`); an empty result means the proposal is valid.
Each `Rejection` carries a stable code, that location, and a message fixed by the code,
never proposal or tool text. Validation reads the run record and the session; it changes
neither.

Citations (`check_citations`) must name records a tool returned for that side's snapshot.
Each piece of cited evidence must belong to a finding or a resource in the same citation, so
evidence about one resource cannot support a claim about another. Analysis results are not
re-checked here: in-memory results pass the analysis integrity checks before they are
delivered, saved results are verified against their snapshots when the session is created,
and the run record keeps detached copies of what was delivered.

Claims (`check_claims`) may be no stronger than the cited findings: `supported` only if
every cited finding is supported, `insufficient_evidence` if every one is, `partial`
otherwise. Without cited findings, recorded differences allow at most `partial`, and
configuration alone only `insufficient_evidence`. Every limitation of a cited finding must
be acknowledged. A cited finding may not leave out a returned finding from the same result,
about the same primary resource, that reaches a different status or network decision.
Narrative may not repeat imported text, whole or in part, or name a recorded ID of at least
`MIN_NAMED_ID_CHARS` characters that no conclusion cites. Imported text is compared
case-folded with whitespace collapsed, by fingerprints of windows of
`MIN_REPEATED_TEXT_CHARS` characters taken every `FINGERPRINT_STRIDE` (8) characters of each
value at least that long. So a narrative is rejected when it repeats a whole such value, or
any stretch of 31 characters of one; a stretch as short as 24 characters is found when it
lines up with a recorded window. Values shorter than a window, such as a resource named
`web`, may be named freely.

| Code | Check | Message |
|---|---|---|
| `answer_side_unavailable` | citations | No comparison snapshot was selected for this session. |
| `answer_source_kind_mismatch` | citations | The source kind differs from the one the tools reported for this snapshot. |
| `answer_unknown_id` | citations | This ID is not recorded in the snapshot for this side. |
| `answer_other_snapshot_id` | citations | This ID belongs to the other selected snapshot; cite it on that side. |
| `answer_id_not_returned` | citations | No tool returned this ID in this conversation. |
| `answer_evidence_detached` | citations | This evidence belongs to neither a finding nor a resource cited alongside it. |
| `answer_planned_as_deployed` | citations | This snapshot records planned or declared configuration; do not describe it as observed. |
| `answer_status_upgrade` | claims | The status is stronger than the cited findings support. |
| `answer_limitation_unacknowledged` | claims | This finding has limitations that the answer does not acknowledge. |
| `answer_limitation_unknown` | claims | This finding has no limitation with this code. |
| `answer_contradiction_omitted` | claims | A returned finding about the same resource from the same result reaches a different conclusion and is not cited. |
| `answer_narrative_imported_text` | claims | This narrative repeats text imported from cloud configuration or source files. |
| `answer_narrative_uncited_id` | claims | This narrative names a recorded ID that no conclusion cites. |

`answer_planned_as_deployed` replaces `answer_source_kind_mismatch` when a Terraform state,
Terraform plan, or CloudFormation snapshot is described as observed.

Transcripts record a proposal with its narrative redacted and bounded and any
secret-looking cited ID replaced by a unique placeholder; the run result keeps the raw
proposal for validation.

## Rendered conclusions

`render_answer(proposal, session, outcome)` (`assistant/answer_rendering.py`) turns a
validated proposal into a `RenderedAnswer` (`assistant/answer_results.py`). The proposal
only selects records; every fact comes from them:

| Conclusion kind | Rendered from |
|---|---|
| `analyzer_finding` | each cited finding's `FindingView`, exactly as a tool delivered it (summary, status, resources, evidence, path, and limitations), and its analyzer |
| `recorded_configuration` | each cited evidence item's configuration path and value, and each cited resource's type, name, and planned actions, from the session's index |
| `recorded_difference` | the entries of the user-selected saved comparisons that name a cited resource: matched pairs and their field differences, unmatched resources, and ambiguous matches |

Every conclusion lists the cited findings, evidence, and resources whatever its kind, keeps
the proposal's citations, and, when it cites findings, carries their combined status: the
same rule as validation. Statements are fixed templates. Record values (summaries, names,
paths, configuration values) are quoted as untrusted data and referred to as `[n]`, so a
model cannot change how a fact is worded, and the contract rejects a reference to data that
is not there. At most `MAX_RENDERED_ENTRIES` (20) comparison entries, and as many field
differences, are shown per conclusion; the rest are counted. The run record keeps the first
view delivered for each finding, since a finding's ID does not depend on its limitation
wording.

Answer-wide qualifications say when a snapshot records declared or planned state, when the
answer or a cited finding is partial, when a cited finding's collection coverage was
incomplete, and that recorded differences are not proof of change or drift. An
`insufficient_evidence` answer says what was checked, never what is true: its reason, and
how many findings from which analyzers the run retrieved. The narrative appears only as the
labelled `summary` and `explanation`, with secret-looking text redacted as in transcripts.

## Failure explanations

`render_failure(session, outcome)` explains a failed `AnswerOutcome` from the outcome and
the run record alone. It takes no proposal and no provider, so it can show neither a
rejected narrative nor provider text, and it makes no provider call. Its `RenderedFailure`
holds:

- `retrieved`: every finding the run retrieved, rendered as in an answer but labelled as
  retrieved, one conclusion per side and analyzer (a snapshot compared with itself counts
  as the primary side) with at most `MAX_CITED_IDS` findings each, up to `MAX_RETRIEVED` (100) conclusions; further findings are counted;
- `unverified`: fixed statements of why the run stopped, then that no answer was
  validated, what the run retrieved (or that it retrieved nothing), and, per snapshot, how
  many collection operations did not succeed, by status.

| Failure reason | Stated as |
|---|---|
| `rejected_after_repair` | the rejection after one repair, then each rejection's location, code, and fixed message, up to `MAX_LISTED_REJECTIONS` (40), with the rest counted |
| `no_proposal_after_repair` | a plain-text answer after one repair, whose text is not shown |
| `malformed_output` | a fixed sentence for the malformed-output reason |
| `provider_error` | the provider error code and its fixed message |
| `limit_reached` | the limit that stopped the run |
| `cancelled` | the cancellation |

Declared and planned snapshots are qualified as they are in answers.

## Repair and the answer service

`answer_question_async(question, provider=, session=, provider_kind=, model=, limits=,
registry=, clock=)` (`assistant/answer_service.py`) runs the
[tool-calling loop](assistant-orchestration.md) with a repair callback, validates the
proposal, and returns an `AnswerResult`: the `AnswerOutcome`, the `RenderedAnswer`, the
transcript (repair turn included), and the IDs tools returned. `answer_question` runs it
from synchronous code, refused inside a running event loop; cancelling the task raises
`AnswerCancelled`, whose `result` explains the cancelled run.

A repair is offered once, for a rejected proposal, a plain-text answer, or a malformed,
invalid, or mixed `submit_answer` call; limits, provider errors, cancellation, and other
malformed output fail without one. The prompt (`assistant/repair.py`) carries only fixed
text:

| Run ended with | Repair prompt |
|---|---|
| a rejected proposal | a tool result for the `submit_answer` call listing each rejection's code, location, and fixed message, up to `MAX_LISTED_REJECTIONS` with the rest counted |
| a malformed, invalid, or mixed submission | a tool result with the reason and a fixed explanation; other calls in a mixed response are answered as not run |
| a plain-text answer | a user message saying only that no proposal was submitted; the text is never quoted |

The repaired proposal is validated again: valid is `answered_after_repair`, rejected is
`rejected_after_repair`, and plain text is `no_proposal_after_repair`. The answer is then
rendered from records, or the failure explained, as described above.

## Outcomes and exit codes

Every answer ends with one `AnswerOutcome` (`assistant/answer_results.py`) whose status is
stable for scripts. A validated `insufficient_evidence` answer is a success: it is what the
evidence supports, and the JSON carries the status for scripts that need it. Exit codes 1–4
keep their existing CLI meanings for errors before a run starts.

| Status | Failure reason | Exit code | When |
|---|---|---|---|
| `answered` | | 0 | the first proposal was valid |
| `answered_after_repair` | | 0 | the proposal after the one repair was valid |
| `failed` | `rejected_after_repair` | 5 | the proposal after the repair was rejected again; the outcome holds its rejections |
| `failed` | `no_proposal_after_repair` | 5 | the model answered in plain text after the repair |
| `failed` | `malformed_output` | 5 | the output could not be used; the outcome holds the malformed-output reason |
| `failed` | `provider_error` | 6 | a provider error stopped the run; the outcome holds its code, and a script may retry |
| `failed` | `limit_reached` | 6 | a run limit stopped the run; the outcome holds the limit, and a script may retry |
| `failed` | `cancelled` | 130 | the run was cancelled, as for an interrupted command |

A failure is never a stack trace or provider text: it is the deterministic explanation
described above, built from the run record alone.

## Renderings

`src/archdiscov/assistant/answer_output.py` presents a `RenderedAnswer` in the style of the
CLI output modules; the `ask` command prints it through `cli_answer_output.py`, and the
[MCP server](mcp-server.md) returns the same forms. Both forms come from the rendered
answer alone, every string is redacted of secret-looking text on the way out, and readable lines
lose control characters such as terminal escapes.

The readable form (`answer_text`, `print_answer`) shows, in order, the outcome and exit
code, each snapshot with its source kind, whether it records observed or declared state,
and its collection window, the status, the qualifications, and then either the
conclusions or, for a failure, what was not verified and the findings retrieved, labelled
as not an answer. Each conclusion lists its statements, the quoted data they refer to as
`[n]`, its findings with their limitations, and its citations; the model's explanation
follows under its own label. Imported text is always JSON-quoted on one line after
`imported:`, and every line of the model's explanation starts with `|`, so neither can pass
for a heading or a conclusion.

The JSON form (`answer_data`) is a contract:

| Key | Holds |
|---|---|
| `schema_version` | `1`; changes whenever the shape does |
| `exit_code` | the process exit code for the outcome |
| `answer` | the `RenderedAnswer`, which validates back into the contract |

| `answer` key | Holds |
|---|---|
| `outcome` | the `AnswerOutcome`: status, failure reason, and its one detail |
| `snapshots` | each side's snapshot ID, source kind, collection window, and `declared` |
| `status` | the validated status, for answers |
| `conclusions` | statements, quoted `data` (each `{"untrusted_text", "truncated"}`), findings, combined status, and citations |
| `qualifications` | answer-wide fixed statements |
| `narrative` | the model's `summary` and `explanation`, marked `"source": "model"` |
| `failure` | for failures: the reason, the `retrieved` conclusions, and the `unverified` statements |

## The ask service

`ask_question(question, snapshot_path=, config_path=, comparison_path=,
comparison_result_paths=)` in `services/ask.py` (with `ask_question_async`) is what the
`ask` command calls. It refuses an empty, blank, or oversized question as
`invalid_question` (2) before anything else, then builds the provider from the
configuration before reading any snapshot, loads the snapshots and any saved comparisons
of them into a tool session, takes the run limits from the configuration, and runs the
answer service. Its `AskResult` (in `assistant/ask_results.py`, beside the answer
contracts) holds the `AnswerResult` and a `RunSummary`: each
snapshot as rendered, the provider kind and model, the limits, and the tool usage from the
transcript (provider requests, tool calls by name, whether the repair was requested, and
token usage). Cancelling raises `AskCancelled`, whose `result` explains the cancelled run;
the synchronous `ask_question` turns an interrupt (Ctrl-C) into the same `AskCancelled`.

With `transcript_path=`, the service publishes the run's `RunTranscript` JSON (already
redacted and bounded) at that path, atomically and never overwriting. Before the run starts,
and so before any provider request, it refuses an existing path, including a dangling symlink,
as `snapshot_conflict` (3) and a path whose parent directory is missing as `snapshot_io_error`
(1). Every run that starts writes its transcript, failed and cancelled runs included; a run
that never starts writes none. If saving fails after the run, the save error is raised, except
on cancellation, where it is logged so the cancellation still propagates.

Errors before the run starts keep the existing CLI codes: an unreadable configuration,
transcript, snapshot, or saved comparison file is 1, and invalid configuration, transcripts,
snapshots, or saved comparisons, including a Bedrock profile that cannot be loaded and a saved
comparison of snapshots that were not selected (`result_snapshot_unselected`), are 2. A saved
comparison's diagnostic names its own file. Once the run starts, everything is an
answer outcome with the exit codes above, except a fake scripting error (`FakeScriptError`,
such as a transcript with no turn left), which is raised as a test failure, exits 2, and
writes no transcript.

## The ask command

`archdiscov ask QUESTION --snapshot PATH --config PATH [--compare PATH]
[--comparison-result PATH ...] [--transcript PATH] [--json]` (`cli_ask.py`) is presentation
over the ask service: the run summary, then the renderings above. Its options, output, JSON
envelope, configuration, transcripts, and exit codes are described in
[the ask command](ask-command.md).
