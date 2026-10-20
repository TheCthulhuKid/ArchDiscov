# Offline snapshot and raw-source ingestion

Step 21 adds explicit raw-source selection to `archdiscov ingest`; the phase-2 acceptance
workflow is in the [README](../README.md#phase-2-offline-acceptance). Run commands from the checkout after locked
installation. No AWS credentials, network access, Terraform/provider execution,
infrastructure apply, stack deployment, or arbitrary template code evaluation is used.

## Formats and destinations

```text
archdiscov ingest INPUT -o NEW --format snapshot|terraform-state|terraform-plan|cloudformation
```

`--output` is the long form of `-o`. Its parent directory must exist, and the
destination must be new: files, directories, and symlinks are never overwritten.
All formats are explicit; extensions and content never select an adapter.
Omitting `--format` means `snapshot`, preserving historical snapshot imports.

| Format | Accepted input | Meaning |
| --- | --- | --- |
| `snapshot` (default) | Archdiscov snapshot JSON, schema `1` or `2` | Validate/sanitize and save existing UUID, source, evidence, and coverage |
| `terraform-state` | Public `terraform show -json` state export | Exported configuration, not live observations |
| `terraform-plan` | Public `terraform show -json` plan export | Planned changes, not applied state |
| `cloudformation` | Local CloudFormation JSON or YAML template | Declarations, not deployed observations |

Internal `.tfstate`, HCL, raw AWS API exports, and backend configuration are not
alternative inputs. Terraform state and plan formats are distinct, not fallbacks.

```bash
mkdir -p snapshots
.venv/bin/python -m archdiscov ingest tests/fixtures/snapshots/partial-coverage.json \
  -o snapshots/imported-snapshot.json
.venv/bin/python -m archdiscov ingest tests/fixtures/terraform/state.json \
  -o snapshots/imported-state.json --format terraform-state \
  --account-id 123456789012 --region eu-west-1 --workspace development --json
.venv/bin/python -m archdiscov ingest tests/fixtures/terraform/plan.json \
  -o snapshots/imported-plan.json --format terraform-plan \
  --account-id 123456789012 --region eu-west-1
.venv/bin/python -m archdiscov ingest tests/fixtures/cloudformation/template.yaml \
  -o snapshots/imported-template.json --format cloudformation \
  --account-id 123456789012 --region eu-west-1 --stack-name demo
```

Choose different output paths before repeating these examples. The installed
`archdiscov` entry point accepts the same arguments.

## Context and source-specific flags

| Flag | Snapshot | Terraform state/plan | CloudFormation |
| --- | --- | --- | --- |
| `--account-id` | Rejected | Optional if unambiguously inferred | Required |
| `--region` | Rejected | Optional if unambiguously inferred | Required |
| `--workspace` | Rejected | Optional descriptive metadata | Rejected |
| `--stack-name` / `--stack-id` | Rejected | Rejected | Optional stack context |
| `--parameters-file` | Rejected | Rejected | Optional local parameter object |
| `--sensitive-path` (repeatable) | Rejected | Rejected | Optional template-relative JSON Pointer |

Irrelevant source-specific flags are rejected with a safe `invalid_request`, not
silently ignored. Account IDs are 12 digits; request regions must be concrete,
not `global`. Terraform infers context only from supported source facts and rejects
missing, ambiguous, conflicting, or mixed-account scope. An empty Terraform export
requires explicit account and region. Workspace never selects a backend.

CloudFormation requires account and region even when stack context is supplied;
it never looks up deployment context. A stack ARN must agree with account, region,
partition, and any supplied stack name. Stack context scopes logical identities;
physical resource IDs are not invented.

## Protected parameters and sensitive paths

`--parameters-file` reads a bounded, strict UTF-8 JSON **object** whose keys are
parameter names and whose values are JSON values. It is not the AWS CLI
`[{"ParameterKey": ..., "ParameterValue": ...}]` list format. For example, a
nonsecret parameter file could contain:

```json
{"Stage": "test"}
```

Supply an existing protected local file by path:

```bash
.venv/bin/python -m archdiscov ingest template.yaml \
  -o snapshots/protected-template.json --format cloudformation \
  --account-id 123456789012 --region eu-west-1 \
  --parameters-file private/parameters.json \
  --sensitive-path /Resources/Server/Properties/UserData \
  --sensitive-path /Parameters/Opaque/Default
```

The sensitive paths must identify permitted values in that template; adapt these
illustrative paths to your input. JSON Pointer escaping uses `~0` for `~` and `~1`
for `/`. Paths may protect resource properties, parameter defaults, mappings, and
conditions, not structural identities such as logical IDs or resource types.

There is deliberately no inline parameter-value flag. Prepare files without echoing
secrets in shell commands, logs, or terminal output; restrict file access and keep
them out of version control. Parameters must be declared and match supported template
types. Use `NoEcho` for secret parameters and explicit sensitive paths for protected
template literals: arbitrary secrets in otherwise allowed names cannot be inferred
reliably. Do not put secrets in identifiers, input paths, workspace, or stack context.
Diagnostics do not render parameter values or raw parser/validation exception text.

## Results, completeness, and failures

`--json` returns one success object on standard output. Existing `snapshot_id` and
`path` fields are preserved; additive fields are:

- `complete`: boolean derived from ingestion coverage, not an architecture finding.
- `source_kind`: the snapshot's kind, such as `terraform_state`, `terraform_plan`,
  or `cloudformation`. Snapshot imports preserve their existing source kind;
  CLI format spellings use hyphens, while Terraform source kinds use underscores.
- `operations`: coverage summaries identifying region, service, operation, status,
  count, and failure codes; the saved snapshot carries account context. Counts describe retained source records,
  including limited summaries where applicable, not proof of full configuration.

Readable success retains `Saved snapshot ...` and qualifies completeness. Partial
results include `PARTIAL (see operation coverage)`. Each recorded operation is also
listed with its scope, status, count when available, and failure codes. A valid partial import is saved
and exits `0` with `complete: false`; unknown values, redactions, unsupported mappings,
and unresolved references remain visible limitations, not invented facts. Inspect
the saved snapshot's operation coverage and evidence before making conclusions.
Completeness concerns the recorded ingestion scope, not deployment, reachability,
effective access, or analysis completeness. Missing operation records make no claim
of successful collection.

| Exit | Meaning |
| --- | --- |
| `0` | Saved successfully, including partial results |
| `1` | Snapshot filesystem/read/write failure |
| `2` | Invalid raw-source format, shape, context, or parameter file (`invalid_request`); existing snapshot validation/JSON/encoding/version errors; CLI usage errors |
| `3` | Destination conflict; existing bytes remain unchanged |
| `4` | Existing `evidence` command: evidence ID not found |
| `5` | Existing live `collect` ingestion/identity failures; unchanged |

Raw adapters retain their existing error taxonomy: a missing or unreadable raw
source file is `invalid_request` with exit `2`, not snapshot filesystem exit `1`.
Default snapshot loading/saving keeps its existing error classifications.
Service errors with `--json` are a single `error` object on standard output with
safe public code/message and applicable diagnostic fields, never raw input values
or tracebacks. Readable errors go to standard error. Typer argument/option usage
errors remain readable and exit `2`, even with `--json`.

## Public Python service

`IngestFormat`, `IngestResult`, and `ingest_snapshot` are exported from
`archdiscov.services`. The service contract is:

```python
def ingest_snapshot(
    input_path,
    output,
    *,
    source_format=IngestFormat.SNAPSHOT,
    account_id=None,
    region=None,
    workspace=None,
    stack_name=None,
    stack_id=None,
    parameters_file=None,
    sensitive_paths=(),
    context=None,
) -> IngestResult: ...
```

`IngestFormat` values are `snapshot`, `terraform-state`, `terraform-plan`, and
`cloudformation`. `IngestResult` contains `snapshot`, `path`, and `complete`.
The same source-specific validation applies to Python callers. Raw formats build
the existing typed adapter request, call `collect_snapshots` with optional injected
`context`, then save through `save_snapshot`. Default snapshot ingestion uses
`load_snapshot` / `save_snapshot`; it does not recollect or reinterpret the source.

```python
from archdiscov.services import IngestFormat, ingest_snapshot

result = ingest_snapshot(
    "tests/fixtures/terraform/state.json",
    "snapshots/service-state.json",
    source_format=IngestFormat.TERRAFORM_STATE,
    account_id="123456789012",
    region="eu-west-1",
)
print(result.path, result.complete)
```

## Mapping and evidence limitations

This command exposes existing adapters; it does not expand their parsing or mappings:

- [Terraform state](terraform-state-mapping.md): allowlisted facts, ownership/scope
  inference, exported references, sensitive masks, and omitted rules/routes/policies.
- [Terraform plan](terraform-plan-mapping.md): lifecycle/action order, before/after
  value scopes, unknown and sensitive values, and bounded exported references.
- [CloudFormation](cloudformation-mapping.md): supported local intrinsic evaluation,
  NoEcho propagation, declaration identities, safe parsing bounds, and unsupported
  intrinsics, nested-stack contents, external imports, and physical values.

Those mapping guides describe adapter-level contracts. No source is treated as live
AWS observation merely because it was saved successfully. Evidence inspection uses the
existing `archdiscov evidence ID --snapshot PATH` command for every source kind.