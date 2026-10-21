# Assistant tools

This page describes the bounded, typed tools under `src/archdiscov/assistant/tools/` that a
future assistant will call. The tools call the same query and analysis services as the
CLI, over data the user selected, and return allowlisted views with explicit truncation.
No command uses them yet: the tool-calling loop, answer validation, and the `ask` command
are later steps, and the only provider is the scripted fake described in
[assistant provider contracts](assistant-providers.md).

## Tool set

The set is fixed and declared in this order (`tools/catalog.py`). Inputs marked in bold are
required; every other input is optional. `limit` defaults to 25 and is capped by the
session's result limit.

| Tool | Inputs | Returns |
|---|---|---|
| `search_resources` | `resource_id`, `name`, `native_type`, `tag_key`, `tag_value`, `physical_id`, `limit` | resources matching exact filters (AND), with limitations; `physical_id` requires `native_type` |
| `inspect_resource` | **`resource_id`** | one resource: fields and attributes, cited facts, owner evidence, contextual coverage, limitations, parent, and children |
| `get_neighbours` | **`resource_id`**, `direction`, `edges`, `relationships`, `attachment_kinds`, `limit` | adjacent recorded edges; nonlocal targets are terminals, never resources |
| `find_paths` | **`resource_id`**, `direction`, `edges`, `relationships`, `attachment_kinds`, `limit`, `depth` | structural paths, shortest first; not reachability or access |
| `inspect_coverage` | `service`, `location`, `status`, `limit` | recorded collection outcomes and counts per status |
| `get_evidence` | **`evidence_id`** | one evidence record, the facts it supports, and same-context coverage |
| `list_analyzers` | none | the five analyzers, their versions, and the parameters `run_analyzer` accepts |
| `run_analyzer` | **`kind`**, `resource_id`, `port`, `max_depth`, `ownership_keys`, `limit` | findings of one analyzer, run in memory; nothing is saved |
| `list_saved_results` | none | the saved analyses and comparisons the user selected, numbered |
| `get_saved_analysis` | **`number`**, `limit` | one selected saved analysis |
| `get_saved_comparison` | **`number`**, `limit` | one selected saved comparison; recorded differences, not drift |

Search, inspection, neighbours, and paths call the step 25 query services exactly as the
`query` commands do. `run_analyzer` calls `run_analyzer` and then
`validate_analysis_result`, the checks `analyze` applies before saving. It rejects
parameters the selected analyzer does not accept, caps `max_depth` by the session depth,
and asks each analyzer for its own maximum of findings so the tool can count what it drops.
Analyzers that do not support the snapshot's cloud return their unsupported result.

Every tool input rejects unknown fields, and every string, number, and list is bounded. No
tool takes a path, file, directory, URL, or snapshot selector; no tool writes files, opens
network connections, or runs commands. Tests derive these checks from the declared schemas,
so a new tool fails them until it complies.

## Sessions

A `ToolSession` is created from user input only: one snapshot, optionally a second for
comparisons, and any saved analysis results and comparisons. Each snapshot is indexed once.
Each saved result is verified against the selected snapshot it names when the session is
created; a tampered result fails with the existing safe validation error, and a result for
an unselected snapshot is rejected. The model reads saved results by their number in
`list_saved_results`, and gets `tool_unavailable` when none were selected.

## Limits and truncation

`ToolLimits` bounds every call in a session.

| Limit | Default | Allowed |
|---|---|---|
| `result_limit` | 25 | 1–100 |
| `depth` | 3 | 1–5 |
| `max_output_chars` | 32,768 | 1,024–262,144 |

Lists that stop short carry a `truncation` with the reasons, the `returned` count, and the
`available` count when it is known.

| Truncation reason | Meaning |
|---|---|
| `result_limit` | more items exist than the request or session limit allows |
| `depth_limit` | path exploration stopped at the depth limit |
| `work_limit` | path exploration stopped at the service's work budget |
| `size_limit` | lists were shortened to keep the output within `max_output_chars` |

When an output would exceed `max_output_chars`, its lists shrink together until it fits;
an output that still does not fit becomes `tool_output_too_large`.

## Errors

Tools never raise for model mistakes or failures. Errors are results with a code, a
message fixed by that code, and for invalid arguments the declared field names that
failed (`locations`), never the model's input.

| Code | Message |
|---|---|
| `tool_malformed_arguments` | Tool arguments were not a JSON object within the size limit. |
| `tool_invalid_arguments` | Tool arguments did not match the tool's input schema. |
| `tool_unknown` | No tool with that name is available. |
| `tool_not_found` | No recorded item has that ID in the selected snapshot. |
| `tool_unavailable` | This tool needs data that was not selected for this session. |
| `tool_output_too_large` | Tool output exceeded the size limit; narrow the request or lower its limit. |
| `tool_internal_error` | The tool failed unexpectedly. |

## Untrusted text

Outputs contain only declared fields. Stable IDs, enums, numbers, and identity parts that
are already pattern-validated (account, subscription, region, location, and service) are
plain values. Everything taken from cloud configuration or source files is wrapped as
`{"untrusted_text": ..., "truncated": ...}`: names, tags, physical IDs, logical addresses,
native types, attribute and evidence values, locators and configuration paths, fact
paths, coverage reasons and failures, terminal identifiers and reasons, finding summaries,
and limitation messages. That text is data to quote, never instructions. It is truncated to
200 characters (`truncated` is then true), and line breaks, control characters, and
invisible format characters such as zero-width spaces and bidirectional overrides become
spaces. Sensitive values are redacted as they are in snapshots.

## Run record

Each session has a `RunRecord` of every ID a delivered output returned, for answer
validation in a later step. It collects the values of the `finding_id`, `finding_ids`,
`evidence_id`, `evidence_ids`, `resource_id`, and `resource_ids` fields (one ID, a list, or
a bounded list), each scoped to the nearest enclosing `snapshot_id`, so comparison IDs are
attributed to their own side. Errors and replaced oversized outputs record nothing.
`returned()` is a frozen, sorted, deduplicated copy per snapshot, unaffected by later
calls.
