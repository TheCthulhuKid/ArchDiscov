# Assistant answers

This page describes the structured answer a model submits at the end of an
[orchestrated run](assistant-orchestration.md), how it is validated against what the
[tools](assistant-tools.md) returned, and how its conclusions are written from records. No
command uses this yet, and the only provider is the scripted fake: the readable and JSON
renderings, failure explanations, the single repair attempt, the `ask` command, and real
providers are later steps.

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
Narrative may not repeat imported text of at least `MIN_REPEATED_TEXT_CHARS` characters
(compared case-folded with whitespace collapsed, by fingerprint) or name a recorded ID of
at least `MIN_NAMED_ID_CHARS` characters that no conclusion cites.

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
