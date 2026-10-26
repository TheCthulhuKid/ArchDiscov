# Ingest and evidence commands

The offline `ingest` and `evidence` commands, their JSON output, and their exit codes. The
[README](../README.md#commands) lists every command; the other commands are described in their
own pages.

[Testing the CLI locally](local-cli-testing.md) walks through every command offline
with the committed fixtures, including `ask` with the fake provider.

Import the project's **own snapshot JSON**, then inspect an evidence-map ID in the saved file:

```bash
mkdir -p snapshots
uv run --locked archdiscov ingest tests/fixtures/snapshots/partial-coverage.json \
  --output snapshots/example.json
uv run --locked archdiscov evidence sg-rule --snapshot snapshots/example.json
uv run --locked archdiscov evidence sg-rule --snapshot snapshots/example.json --json
```

The offline Azure fixture uses the same commands, without Azure credentials or SDK calls:

```bash
mkdir -p snapshots
uv run --locked archdiscov ingest tests/fixtures/snapshots/azure.json \
  --output snapshots/azure-example.json
uv run --locked archdiscov evidence vm-size --snapshot snapshots/azure-example.json
uv run --locked archdiscov evidence vm-unavailable \
  --snapshot snapshots/azure-example.json --json
```

`vm-size` cites the present `/vm_size` null fact; `vm-unavailable` records a separate
unresolved limitation, not an invented fact. Azure output uses subscription context,
`location`, and native service namespaces, not AWS region enums. Choose fresh paths
if either example has already been run.

The same arguments work with `python -m archdiscov`. `--output` (`-o`) and `--snapshot`
are required explicit paths; parents must exist. Ingestion validates and sanitizes the
input, preserving its UUID, typed children, citations, and coverage. It never replaces
an existing destination, including identical snapshots and symlinks. Choose a new path
for each import. `--format snapshot` is the default and preserves this workflow; formats
are never guessed from filenames or contents. Raw AWS exports are not accepted.

For offline IaC input, select an explicit format and a fresh destination:

```bash
uv run --locked archdiscov ingest tests/fixtures/terraform/state.json \
  -o snapshots/state-example.json --format terraform-state \
  --account-id 123456789012 --region eu-west-1
uv run --locked archdiscov ingest tests/fixtures/terraform/plan.json \
  -o snapshots/plan-example.json --format terraform-plan \
  --account-id 123456789012 --region eu-west-1
uv run --locked archdiscov ingest tests/fixtures/cloudformation/template.yaml \
  -o snapshots/template-example.json --format cloudformation \
  --account-id 123456789012 --region eu-west-1 --stack-name demo
```

Terraform account/region context may be omitted only when source facts resolve it
unambiguously; CloudFormation requires both. `--workspace` is Terraform-only;
`--stack-name`, `--stack-id`, `--parameters-file`, and repeatable `--sensitive-path`
are CloudFormation-only. Irrelevant source-specific flags fail with `invalid_request`.
Parameter files are bounded strict UTF-8 JSON objects mapping parameter names to JSON
values, not AWS `ParameterKey` lists. Keep secrets in protected local files: there is
no inline parameter-value flag, and parameter values must not be echoed to the shell.
See [raw-source ingestion](raw-source-ingestion.md) for the full flag matrix,
Python service, sensitivity guidance, output contract, and adapter limitations.

Evidence inspection shows the selected path/UUID and snapshot source, associated resource,
source operation/file, configuration path, explicit fact citations, sanitized value/status,
redaction paths, and omitted/unresolved-value limitations. It also shows all recorded
operation outcomes for the owner's region and service, including failures, reasons,
unresolved values, and successful zero results. This is **coverage context**, not a claim
that each outcome produced the record or proof of complete collection; missing outcomes
make no completeness claim. Inspection does not infer relationships or architecture findings.
Evidence IDs are scoped to the selected file, and even uncited stored records are inspectable.

Both commands accept `--json`. Success writes a single JSON object to standard output:
ingestion preserves `snapshot_id` and `path`, adding `complete`, `source_kind`, and an
`operations` list of operation scope, status, count, and failure codes; evidence returns `snapshot_path`, `snapshot_id`,
`snapshot_source`, `evidence_id`, `resource`, `evidence`, `fact_paths`, and `coverage`.
Saved partial imports still exit `0` with `complete: false`. Readable ingestion retains
`Saved snapshot ...` and qualifies completeness, including `PARTIAL (see operation coverage)`
for partial results. This reports ingestion coverage, not architecture analysis.
Readable ingestion also lists recorded operations with their scope, status, available counts,
and failure codes, using the same presentation as collection.
Service failures in JSON mode also write a single object to standard output, with an `error`
containing `code`, `message`, optional `path`, validation `details`, or JSON syntax
`line`/`column`. No raw input values, exception contexts, or tracebacks are rendered.
Readable errors go to standard error. Argument/option parsing errors retain Typer's
standard readable usage diagnostics, even with `--json`; help/version remain readable.

| Exit code | Meaning |
| --- | --- |
| `0` | Saved successfully, including partial ingestion; inspect completeness and operation coverage |
| `1` | Snapshot filesystem/read/write failure |
| `2` | Invalid JSON/encoding/snapshot, unsupported version, invalid raw-source format/shape/context (`invalid_request`), or CLI usage error |
| `3` | Destination already exists |
| `4` | Evidence ID is absent from the selected snapshot |

Raw-adapter input-read failures, including missing files, retain their existing
`invalid_request` classification and exit `2`, not snapshot filesystem exit `1`.
Live `collect` ingestion/identity failure exit `5` is unchanged.

Python callers can use `get_evidence(snapshot, evidence_id)` from `archdiscov.services`.
It returns a detached `EvidenceInspection` after revalidation and raises
`EvidenceNotFoundError` (a `SnapshotError`, code `evidence_not_found`) for an absent ID.
JSON data is available through `result.model_dump(mode="json")`; the CLI only supplies
file selection and presentation around this service and the persistence APIs.
