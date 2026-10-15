# Archdiscov: Evidence-backed cloud architecture snapshots

## Summary

Archdiscov is a Python CLI and reusable core for collecting AWS architecture configuration,
saving validated evidence-backed snapshots, and inspecting their facts and limitations.
It supports explicitly configured accounts, enabled-region discovery, and bounded async
collection across seven service families, with one immutable snapshot file per account.
The cloud-neutral snapshot boundary also supports offline Azure fixture ingestion and
evidence inspection; it does not provide live Azure collection or analysis.

### Current state

| Capability | Status |
| --- | --- |
| Snapshot models, evidence/fact citations, coverage, sanitization, and schema `1`–`3` validation | Implemented; legacy AWS schemas `1`/`2` unchanged |
| Local no-overwrite persistence, snapshot JSON `ingest`, and `evidence` inspection | Implemented |
| Cloud scopes/identities, schema `3`, explicit AWS upgrade, and detached `SnapshotAccess` | Implemented; [contracts and APIs](docs/cloud-snapshots.md) |
| Azure VNet/subnet/VM snapshots | Offline fixture only; no Azure SDK, authentication, or live collector |
| EC2/network, IAM, ALB/NLB, ECS, Lambda, RDS/Aurora, and SQS configuration collection | Implemented |
| Multi-account `collect --all-services`, enabled-region discovery, shared call budgets, async APIs, and cancellation-safe draining | Implemented |
| Offline Terraform `show -json` state import through Python services | Implemented; [mapping and limitations](docs/terraform-state-mapping.md) |
| Offline Terraform `show -json` plan import through Python services | Implemented; [mapping and limitations](docs/terraform-plan-mapping.md) |
| Offline CloudFormation JSON/YAML import through Python services | Implemented; [mapping and limitations](docs/cloudformation-mapping.md) |
| Explicit snapshot/Terraform state/Terraform plan/CloudFormation CLI ingestion | Implemented (step 21); [usage and contracts](docs/raw-source-ingestion.md) |
| Phase 2 permissions, setup, and offline acceptance | [Consolidated permissions](docs/aws-permissions.md) and [acceptance workflow](#phase-2-offline-acceptance) |
| Docker development environment and GitHub Actions pytest/Ruff checks | Implemented |
| Analysis result contracts and `Finding`/`Answer` compatibility | Implemented snapshot-backed integrity checks; no analyzers or answer generation |
| Graph resource ownership, lookup, containment, dependencies, and evidence/coverage | [Implemented and verified together across schemas 1–3](docs/graph-index.md#composed-graph-boundary), including external/unresolved terminals and complete-context coverage; queries remain planned |
| Graph queries, comparisons, and LLM assistant/providers | Planned |

[PLAN.md](PLAN.md) records delivery progress and the remaining roadmap. “All services”
means the supported inventory, not every AWS resource type or proof of reachability/effective
access. Start with the [offline workflow](#implemented-offline-workflow), or use
[AWS collection](#aws-configuration-collection-and-the-collect-command) with runtime credentials.

## Installation and development

Use Python 3.13 and uv. From the repository root, install the package and development dependencies using the committed lockfile:

```bash
uv sync --locked --python 3.13
```

The CLI provides help, installed-version output, AWS collection, snapshot and raw IaC ingestion, and evidence inspection:

```bash
uv run --locked archdiscov --help
uv run --locked archdiscov --version
.venv/bin/python -m archdiscov --help
```

Run development checks through the project virtual environment:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check --exclude .junie .
```

These tests require neither AWS/provider credentials nor network calls. Package installation requires access to the dependency registry unless dependencies are already cached.

### Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs the full offline pytest suite and
Ruff lint/format checks in separate jobs on every push and pull request, with manual
runs available too. CI uses Python 3.13, uv 0.5.29, and locked development dependencies;
it requires no AWS/provider secrets. Formatting excludes `.junie` plan artifacts,
matching the local commands above. Superseded runs are cancelled automatically.

The source package separates shared models, ingestion adapters, public application services, and CLI presentation:

```text
src/archdiscov/
├── models/       # Normalized resources, snapshots, dependencies, and evidence
├── ingestion/    # Allowlisted AWS collectors and offline Terraform/CloudFormation adapters
├── services/     # Sync/async collection, persistence, and evidence inspection
├── cli.py        # Typer presentation and entry point
└── __main__.py   # python -m archdiscov
```

Model imports use the installed package, for example `from archdiscov.models.architecture import ArchitectureSnapshot` and `from archdiscov.models.enums import AwsRegion`. CLI commands call application services; discovery and analysis logic belong outside the CLI.

Packaging tests execute both CLI entry points and model imports from a temporary directory outside the repository.

### Docker development

Install Docker with the Compose plugin and BuildKit support. On Linux/WSL, match the container user's IDs to your host user before building:

```bash
export DEV_UID=$(id -u)
export DEV_GID=$(id -g)
```

Alternatively, copy `.env.example` to `.env` and set those values there. Use non-root IDs. Docker Desktop on macOS/Windows can generally use the defaults; on Windows, run these examples from a WSL shell with Docker Desktop integration enabled. Keeping the checkout in the WSL filesystem usually improves bind-mount performance. A devcontainer configuration is deferred.

Build the development image and start an interactive shell:

```bash
docker compose build dev
docker compose run --rm dev
```

Run commands without opening a shell:

```bash
docker compose run --rm dev archdiscov --help
docker compose run --rm dev archdiscov --version
docker compose run --rm dev python -m pytest
docker compose run --rm dev ruff check .
docker compose run --rm dev ruff format --check --exclude .junie .
```

The image uses Python 3.13 and uv 0.5.29. Dependencies, including development tools, are installed from `uv.lock` with frozen resolution. The checkout is mounted at `/workspace`; the editable package reflects source edits immediately. The container uses `/opt/venv`, independent of the host `.venv`, and its `python` resolves to that environment. Source commands and generated snapshots run as the configured non-root user, so files written under `/workspace` remain accessible on the host.

BuildKit caches dependency downloads and the dependency-installation layer. Compose also retains a named uv cache for commands run inside the container. Rebuild after changing `pyproject.toml`, `uv.lock`, the Dockerfile, or user IDs. Run `uv lock` on the host when intentionally changing dependencies, then rebuild so the image matches the committed lockfile. For a temporary container-side dependency refresh, run `uv sync --locked --group dev` inside the development shell; rebuild afterward to make the change available to subsequent containers.

Dependency synchronization can require network access to rebuild the editable package: the runtime cache is separate from BuildKit's build cache. Normal CLI and test commands use the installed environment directly. If changing user IDs makes an existing cache volume unwritable, remove the development cache with `docker compose down --volumes` before rebuilding; this removes the named cache, while the bind-mounted checkout remains on the host.

Normal tests require no network calls. To enforce network isolation when checking the built image, use Docker directly with the same source mount:

```bash
docker run --rm --network none --mount "type=bind,src=$PWD,dst=/workspace" archdiscov-dev python -m pytest
```

### Optional runtime credentials

The default development service supplies no AWS or provider credentials. For live AWS collection, mount your AWS configuration explicitly and choose a profile:

```bash
docker compose run --rm \
  --volume "$HOME/.aws:/home/dev/.aws:ro" \
  --env AWS_PROFILE=development \
  dev bash
```

For SSO, authenticate on the host first with `aws sso login --profile development`. The read-only mount exposes the host configuration and cached tokens; refresh them on the host when needed. Configured assume-role profiles can use the mounted source profiles. Profiles using `credential_process` need the referenced executable and any supporting files inside the container; host-only executables do not automatically become available. Use `archdiscov collect` for the seven implemented service families below.

For future LLM-provider integration (phase 4, separate from cloud platforms), forward a variable already set in your host environment, for example `docker compose run --rm --env OPENAI_API_KEY dev bash`. Credentials belong in runtime configuration and are excluded from the image build context. Keep snapshots in the ignored `snapshots/` directory; committed test fixtures belong under `tests/`.

## Architecture and interfaces

The snapshot, ingestion, persistence, and CLI boundaries below are implemented.
Graph resource ownership, lookup, containment, and dependency adjacency are implemented; queries,
deterministic analyzers, comparisons, and provider interfaces remain planned.

- Organize the application into ingestion adapters, normalized models, graph queries, deterministic analyzers, provider adapters, and CLI presentation.
- Define Pydantic models for `AccountArchitecture`, `RegionArchitecture`, typed resources, `Dependency`, `Evidence`, `Coverage`, `Finding`, and `Answer`. Version serialized snapshots.
- Root each `ArchitectureSnapshot` in one account's ID, name, and metadata. Its `regions` mapping is keyed by AWS region name and contains typed resource lists. Store account-wide resources such as IAM roles separately in `global_resources`; retain unsupported types through an explicit `UnknownResource` fallback.
- Give every resource a generic recursive `children` list inherited from `Resource`, along with its ID, name, type, attributes, and evidence references. Specific models add validated fields such as VPC CIDRs or an EC2 instance type. Regions hold top-level resources; any resource can contain further resources.
- Use the tree for containment, such as VPC → subnet → EC2 instance. Keep VPC-wide and multi-subnet resources as direct VPC children. Store each resource once and express other attachments through dependencies. The generic model validates resource attributes without enforcing AWS-specific parent/child rules.
- Keep dependencies grouped by their source region after the architecture fields, followed by evidence. Use `global` for dependencies originating from account-wide resources. Multi-account collection produces one snapshot per account.
- Persist snapshots as local JSON documents with sanitized source evidence. Build graph indexes in memory; no database or graph server is required initially.
- Give resources stable identities using account, region, service, and AWS identifier. Keep IaC logical addresses when physical identifiers are unavailable.
- Attach evidence references to facts and relationships. Evidence records identify the snapshot, source operation or file, resource, configuration path, and relevant value.
- Track collection coverage by account, region, service, and operation, including failures, unsupported resources, timestamps, and unresolved values.
- Keep live observations, Terraform state, Terraform planned changes, and CloudFormation declarations distinguishable. Compare explicitly selected snapshots; match physical identifiers first and report unmatched resources without guessing from names.

These account/region examples describe the legacy AWS contract. Schema `3` uses
`CloudSnapshot`, an explicit AWS or Azure scope, location-keyed `regions`, separate
`scope_resources`, and flat dependencies. `SnapshotAccess` supplies a common detached
read-only view for both contracts, without graph inference or file migration. See
[cloud snapshots](docs/cloud-snapshots.md) for identities, provenance, and extension rules.

### Resource hierarchy

Each legacy AWS snapshot is rooted in one account. Schema `3` instead has an explicit
cloud scope. Regions contain top-level resources, and every resource can contain further
resources through its `children` list; the following example is AWS:

```text
Account snapshot
├── account_id, name, metadata
├── global_resources
│   └── IAM roles and policies
└── regions
    └── eu-west-1
        └── resources
            ├── VPC
            │   └── children
            │       ├── Subnet
            │       │   └── children
            │       │       └── EC2 instance
            │       ├── Security group
            │       ├── Load balancer
            │       └── Database
            ├── SQS queue
            └── Lambda function outside a VPC
```

VPC-wide and multi-subnet resources appear once under the VPC; dependencies record their subnet attachments. Dependencies and evidence remain separate fields on the account snapshot. The service examples shown have dedicated models; unsupported types use `UnknownResource`, and missing parents are not synthesized.

`Dependency.relationship` uses `DependencyRelationship`: `attached_to`, `forwards_to`, or
`assumes_role`. These normalized relationship kinds serialize as strings in JSON; unsupported
values are rejected. Extend the enum when introducing another relationship kind.

### Snapshot format

All supported schemas require an explicit UUID `snapshot_id`, a timezone-aware collection start/end,
one typed `source`, and `coverage` describing the requested scope. Collection completion
must not precede its start. The optional source export timestamp does not replace the
collection window: it is `source.exported_at` in legacy schemas and
`source.provenance.exported_at` in schema `3`.

| Schema | Model and scope | Compatibility |
| --- | --- | --- |
| `1` | `ArchitectureSnapshot`, AWS account/regions | Strict local dependencies; existing wire format and stable IDs retained |
| `2` | `ArchitectureSnapshot`, AWS account/regions | External/unresolved targets and Terraform-plan lifecycle; AWS collectors and raw importers still emit this schema |
| `3` | `CloudSnapshot`, AWS account/partition or Azure subscription/optional tenant | Explicit platform/provenance, locations, scope-wide resources, context-bearing evidence and coverage |

Public `validate_snapshot`, `load_snapshot`, `save_snapshot`, and snapshot-format
`ingest_snapshot` support all three. Direct `ArchitectureSnapshot` validation still
accepts only `1`/`2` and rejects Azure and `cloud_unknown`. Conversion is explicit through
`upgrade_snapshot`; [the cloud guide](docs/cloud-snapshots.md) documents schema `3`.

Each snapshot represents one scope and one source kind. The following legacy AWS
`source.kind` table also describes schema `3`'s `source.provenance.kind`, except that
schema `3` additionally supports `azure_fixture` with a required offline fixture `label`:

| `source.kind` | Meaning | Source-specific fields |
| --- | --- | --- |
| `aws` | Live observations | Optional `profile`, `role_arn` |
| `terraform_state` | Exported state, not live observations | Required `path`, `format_version`; optional `terraform_version`, `workspace`, `lineage`, nonnegative integer `serial` |
| `terraform_plan` | Planned architecture, not applied state | Required `path`, `format_version`; optional `terraform_version`, `workspace` |
| `cloudformation` | Declarations, not deployed observations | Required `path`; optional `stack_id`, `stack_name`, `template_format_version` |
| `mock` | Synthetic test input | Required `label` |

Source kinds cannot be mixed; compare separate snapshots explicitly. Source-specific fields
are validated rather than hidden in generic `metadata`. File paths identify input provenance,
not permission to open files. Optional profile/role fields identify collection context, not secrets.
`source.kind` uses `SnapshotSourceKind`, with a fixed enum literal for each source model
to preserve discriminated validation. `Evidence.source` uses the separate `SourceType` enum:
`aws`, `terraform`, `cloudformation`, `mock`, or `azure` (schema `3`). Evidence groups Terraform as one source family,
while snapshot kinds distinguish state from plans. Both enums serialize as the existing JSON strings.
When supplied, `role_arn` must be an IAM role ARN such as
`arn:aws:iam::123456789012:role/team/Discovery`. Validation requires an empty region,
a 12-digit account ID, and IAM role-name/path character and length constraints. AWS partition
names such as `aws-cn` and `aws-us-gov` are supported; user and STS assumed-role session ARNs
are not role ARNs. This validates syntax only, not role existence or permission to assume it.

In legacy AWS schemas, `coverage.regions` lists unique requested regions, and `coverage.account_wide` explicitly
requests account-wide collection. At least one region or account-wide scope is required.
`global` is reserved for account-wide resource identity and dependency grouping, not a regional
scope entry. Regional contents must be within the requested regions, and global resources
require account-wide scope. Requested scope is **not proof of successful collection**:
an absent or empty region does not distinguish zero results from failure. Operation-level
outcomes are recorded separately in `coverage.operations`; an absent operation record means
there is no claim about that operation's collection outcome.

### Evidence, fact citations, and collection coverage

Each `Evidence` record requires `snapshot_id`, stable `resource_id`, source family, `locator`
(API operation or input file), and `configuration_path` (the exact source field or rule).
Evidence IDs remain the keys of the snapshot's `evidence` mapping. Resource-level
`evidence_ids` provide broad provenance; `Resource.fact_evidence` maps **resource-relative
JSON Pointers** to nonempty evidence-ID lists for individual normalized fields or rules.
For example, `/attributes/ingress/0` cites a whole rule, while
`/attributes/ingress/0/from_port` cites one field. JSON Pointer escaping uses `~0` for `~`
and `~1` for `/`; dotted model paths are not accepted. Keep source-specific paths in evidence,
not in the normalized fact map. Relationships continue to cite `Dependency.evidence_ids`.
Snapshot validation checks every evidence record, including uncited records: its UUID must
match the snapshot and its owner must be a stored resource. Evidence-map keys and reference
IDs must be nonblank. Resource and fact citations must resolve to evidence owned by the
citing resource; fact citations need not also appear in the broad `evidence_ids` list.
Fact pointers resolve against the resource's JSON-mode representation, including subtype
fields. Dictionary keys must exist; array indices must be canonical nonnegative integers
within bounds (no `-`, negative indices, or leading zeros). Traversal through scalars is
rejected, but a present `null` is a valid target.

`coverage.operations` contains one aggregate `Coverage` per account, region, service, and
operation. Adapters aggregate pagination and retries into that record, rather than recording
each page as a separate outcome. Account must match the snapshot; region must be requested
(or `global` with `account_wide: true`). Timezone-aware `started_at` and `completed_at` must
fall within the snapshot collection window. `result_count` counts source records retained
for that operation, not the total normalized resource tree.

| `status` | Required outcome data | Interpretation |
| --- | --- | --- |
| `success` | Explicit nonnegative integer `result_count`; no failures or unresolved values | Collection completed, including zero results |
| `failed` | Nonempty `failures` with `code` and sanitized `message`; no count | Collection failed without retained results |
| `incomplete` | Explicit retained count and failures, unresolved values, or a `reason` | Partial data; not sufficient to infer absence |
| `skipped` | `reason`; no count, failures, or unresolved values | Intentionally not attempted |
| `unsupported` | `reason`; no count, failures, or unresolved values | Adapter does not support the requested operation/data |

`unresolved_values` records `configuration_path`, `reason`, and optional `resource_id`.
When supplied, `resource_id` must identify a resource stored in this snapshot; absent
resource context remains allowed.
It preserves the location and explanation of unknown IaC values or missing facts without
retaining potentially sensitive raw expressions. Use `incomplete` when an import contains
unknown values, unsupported resource summaries, or a later page fails after results were retained.

The [partial-coverage fixture](tests/fixtures/snapshots/partial-coverage.json) demonstrates a
successfully collected security-group rule allowing TCP 443 from `0.0.0.0/0`, failed
`DescribeRouteTables`, and successful `DescribeInstances` with zero results. It supports
the qualified conclusion that the rule permits that port, **not** that a resource is reachable
from the internet: routing evidence is missing. This is a model example, not an analyzer.

#### Shared sanitization policy

Model validation recursively sanitizes generic resource/dependency `attributes`, account/region
`metadata`, and evidence values. Credential, password, token, API/private-key, authorization,
user-data, and connection-string keys are replaced with `[REDACTED]`; key matching ignores
case and punctuation and recognizes common prefixed secret names. Ordinary network facts
and secret ARN references are retained. Evidence also checks sensitive source path components,
so a scalar password fact is redacted even when its value has no dictionary key.
`archdiscov.models.sanitization.sanitize_value` is the shared adapter helper; `sensitive=True`
redacts the entire value when the source marks it sensitive (for example Terraform sensitive
values or CloudFormation `NoEcho` parameters).

Evidence records `value_status` as `present`, `redacted`, `omitted`, or `unresolved`.
`present` requires an explicit `value`, including a legitimate JSON `null`. `redacted` retains
the sanitized value and `redacted_paths` (JSON Pointers relative to that value; `""` means
the whole value). `omitted` and `unresolved` require a nonempty `limitation` and no non-null
value or redaction paths. Adapters may exclude an entire sensitive value with `omitted`.
Redaction markers and evidence limitations survive JSON round trips.

This is a field-name policy, **not a general secret detector**. Adapters must use source
sensitivity annotations, allowlist collected configuration, and exclude secrets in arbitrarily
named strings, tags, free-form failure messages, reasons, paths, or names before constructing
models. Do not collect secret values or workload data. Mutable models can be changed after
validation; persistence revalidates detached current data before saving.

IaC importers must receive an explicit account and region when the input cannot supply them;
do not invent an account or put regional resources under `global`. Region fields use the
`AwsRegion` enum, including `AwsRegion.GLOBAL` for account-wide identities and dependencies.
New AWS regions must be added to the enum before use; JSON still stores region codes as strings.

AWS resources carry a separate `identity` with account, region (or `global`), service, and at least
one of `physical_id` or `logical_address`. Services use the `AwsService` enum of ARN service
namespaces; extend it when adding another service namespace. For example, load balancers use
`elasticloadbalancing`, not the Boto3 client name `elbv2`. Collection adapters must map service
namespaces to client names explicitly. `ResourceType` values use CloudFormation resource type
identifiers; `unknown` is an internal fallback discriminator, not an AWS type.
The base `Resource.resource_type` also uses `ResourceType`; preserve unsupported AWS type
strings in `UnknownResource.aws_resource_type` rather than passing them to the base model.
Preserve both identifiers when available. Physical identifiers
may be AWS IDs or ARNs; logical addresses should include their module/stack namespace to avoid
collisions (for example `module.network.aws_vpc.main` or `network/MainVpc`).
The normalized resource `id` is derived as
`account/region/service/physical/<identifier>` when a physical identifier exists, otherwise
`account/region/service/logical/<address>`. The final segment is percent-encoded, including
slashes and percent signs. An input may omit `id`; a supplied `id` must match the derived value.
Names, tree positions, and snapshot UUIDs do not affect resource IDs. Acquiring a physical ID
changes a logical-only ID, but the logical address remains available for later comparison.

Resource names are optional (`null` or absent); no name is synthesized. Snapshot validation
indexes every regional and global tree: a stable ID may be stored only once, and every
descendant must match the snapshot account and its enclosing region (or `global`). Duplicate
errors identify both storage locations. AWS-specific parent/child placement and unsupported
type normalization remain adapter responsibilities; containment does not infer attachments.

Local dependency endpoints must exist in the same snapshot, including cross-region
and regional/global links. The grouping key must match the source resource's region;
each dependency's nonempty evidence list must resolve to source-owned evidence. Endpoint
and evidence IDs must be nonblank. Schema `2` additionally supports explicit external
and unresolved targets, never inferred or silently repaired by validation.

Public snapshot services support schema versions `"1"`, `"2"`, and `"3"`. Missing or
other versions are rejected with safe diagnostics, without echoing arbitrary raw values.
There is no automatic file migration or fallback; historical files are never rewritten
implicitly. Direct legacy model validation remains limited to `"1"` and `"2"`.

Schema `1` keeps strict local dependency targets. Schema `2` dependencies may use a
discriminated target: `local` (must resolve to a stored resource), `external` (a known
identifier outside the snapshot, with optional account, region, service, and AWS type), or
`unresolved` (a sanitized reason and source path). Targets never create resources. Schema `2`
also allows `lifecycle` on resources, for Terraform plan snapshots only. Its ordered
`actions` preserve the plan sequence; optional `value_scope` identifies `before` for
pure deletion or `after` otherwise. Historical action-only snapshots remain valid.
The `dependency-targets.json` fixture demonstrates all target kinds.

An account snapshot has this shape:

```json
{
  "schema_version": "1",
  "snapshot_id": "00000000-0000-4000-8000-000000000001",
  "account_id": "123456789012",
  "name": "production",
  "collection_started_at": "2026-10-13T08:00:00Z",
  "collection_completed_at": "2026-10-13T08:01:00Z",
  "source": {"kind": "mock", "label": "hierarchy-example"},
  "coverage": {"regions": ["eu-west-1"], "account_wide": false},
  "regions": {
    "eu-west-1": {
      "resources": [
        {
          "identity": {
            "account_id": "123456789012",
            "region": "eu-west-1",
            "service": "ec2",
            "physical_id": "vpc-example"
          },
          "name": "main",
          "resource_type": "AWS::EC2::VPC",
          "cidr_blocks": ["10.0.0.0/16"],
          "children": [
            {
              "identity": {
                "account_id": "123456789012",
                "region": "eu-west-1",
                "service": "ec2",
                "physical_id": "subnet-example"
              },
              "name": "application",
              "resource_type": "AWS::EC2::Subnet",
              "availability_zone": "eu-west-1a",
              "cidr_blocks": ["10.0.1.0/24"],
              "children": [
                {
                  "identity": {
                    "account_id": "123456789012",
                    "region": "eu-west-1",
                    "service": "ec2",
                    "physical_id": "i-example"
                  },
                  "name": "web",
                  "resource_type": "AWS::EC2::Instance",
                  "instance_type": "t3.micro"
                }
              ]
            }
          ]
        }
      ]
    }
  },
  "global_resources": [],
  "dependencies_by_region": {},
  "evidence": {}
}
```

This example demonstrates structure only; evidence-backed answers require populated evidence and coverage records. Dedicated IAM, EC2 network, ELBv2, ECS, Lambda, RDS/Aurora, and SQS types are implemented. Unsupported types use `UnknownResource`.

Small reusable JSON examples are in [tests/fixtures/snapshots](tests/fixtures/snapshots):

| Fixture | Purpose |
| --- | --- |
| [aws.json](tests/fixtures/snapshots/aws.json) | AWS-source regional and account-wide resources |
| [hierarchy.json](tests/fixtures/snapshots/hierarchy.json) | Typed VPC → subnet → EC2 containment, unknown resources, unnamed resources, and an evidenced dependency |
| [terraform-plan.json](tests/fixtures/snapshots/terraform-plan.json) | Logical-only declaration without a physical ID |
| [cloudformation.json](tests/fixtures/snapshots/cloudformation.json) | Logical address with an escaped slash in its stable ID |
| [partial-coverage.json](tests/fixtures/snapshots/partial-coverage.json) | Cited ingress, denied route-table collection, and successful zero-result instance collection |
| [iam.json](tests/fixtures/snapshots/iam.json) | Typed account-wide role, instance profile, managed policy, trust and inline-policy documents |
| [network.json](tests/fixtures/snapshots/network.json) | Typed network configuration, rule citations, and attachments |
| [dependency-targets.json](tests/fixtures/snapshots/dependency-targets.json) | Schema `2` local, external, and unresolved targets |
| [load-balancing.json](tests/fixtures/snapshots/load-balancing.json) | Typed ELBv2 resources, listener actions/rules, and target-health observations |
| [azure.json](tests/fixtures/snapshots/azure.json) | Schema `3` offline VNet → subnet → VM, explicit fixture attachment, known-null/unavailable facts, and success-zero/incomplete/failed coverage |

Containment never implies an attachment or dependency. Azure fixture provenance is
synthetic, not live observation metadata.

Contract tests also cover Terraform state and mock sources, plus incomplete coverage with
failures, unresolved values, or reasons. Unresolved values describe unavailable facts;
they do not permit dangling local dependency endpoints. Schema `"2"` explicitly represents
external and unresolved relationship targets without inventing local resources.

Snapshot-wide containment, uniqueness, relationship, and evidence integrity checks are
implemented, as are explicit-path local persistence and snapshot JSON CLI ingestion.

To support another resource type, subclass `Resource`, give it a unique `Literal` value for `resource_type`, and add it to the discriminated `SupportedResource` union. The same union validates regional resources, global resources, and every level of `children`, preserving subtype fields when loading and saving JSON. Unsupported types can use `UnknownResource` with their original AWS type and generic attributes; they can also have children.

### Snapshot Python services

Load a selected UTF-8 JSON file or validate supplied snapshot data without CLI commands:

```python
from archdiscov.services import load_snapshot, save_snapshot, validate_snapshot

snapshot = load_snapshot("snapshots/account.json")
validated_copy = validate_snapshot(snapshot)  # Also accepts a mapping of snapshot fields.
saved_path = save_snapshot(validated_copy, "snapshots/new-account.json")
```

Validation returns a fresh, sanitized model and rechecks current mutable model data,
including nested identities and references; it does not invent UUIDs or migrate versions.
Loading applies the same schema, integrity, and sanitization checks.

For common read-only traversal or an explicit legacy-to-schema-`3` conversion, use
`SnapshotAccess` and `upgrade_snapshot`. They return detached data and never rewrite
files; saving an upgrade requires a new path:

```python
from pathlib import Path

from archdiscov.services import SnapshotAccess, load_snapshot, save_snapshot, upgrade_snapshot

legacy = load_snapshot("tests/fixtures/snapshots/hierarchy.json")
upgraded = upgrade_snapshot(legacy)
Path("snapshots").mkdir(exist_ok=True)
save_snapshot(upgraded, "snapshots/aws-schema3-example.json")
access = SnapshotAccess.from_snapshot(legacy)  # Internal normalization; no file migration.
print(access.scope.model_dump(mode="json"))
print([resource.id for resource in access.resources()])
print(list(access.containment()))
print(list(access.dependencies()))
print(list(access.evidence))
print(access.coverage.model_dump(mode="json"))
```

The [cloud guide](docs/cloud-snapshots.md#python-services-and-normalized-access) also
demonstrates location-aware roots and Azure evidence. Choose a fresh output path on reruns.

Saving revalidates detached data and writes normalized, sanitized, indented UTF-8 JSON
with a trailing newline, preserving nulls, defaults, subtype fields, citations, and
coverage. The caller's model is not changed. **Storage immutability means an existing
path cannot be updated**, even with identical content: files, directories, and symlinks
(including dangling ones) are conflicts. Loaded models remain mutable, but changing
them does not change saved bytes. Paths select snapshots, not UUIDs; the same UUID may
be saved at another path. Direct external file edits are outside this guarantee.

The destination parent must already exist. Saving creates a uniquely named temporary
file with restrictive permissions in that directory, flushes and `fsync`s its contents,
then publishes it with an atomic no-overwrite hard link. Successful publication is the
commit point. Temporary-file cleanup is best effort: failures are logged; after commit
the save still returns successfully, and before commit the original error is retained.
A cleanup failure may leave a `.archdiscov-*.tmp` file for manual removal.
Supported local filesystems must allow atomic hard-link publication; unsupported
filesystems fail safely without replacement fallback. This promises complete atomic
visibility and no overwrite, not universal power-loss durability or network-filesystem
guarantees. No parent directories, registry, or migrations are created.

Catch `SnapshotError` for all service errors, or the more specific
`SnapshotValidationError`, `SnapshotJSONError`, `SnapshotConflictError`, and
`SnapshotIOError`. Conflicts use `snapshot_conflict`. Errors expose a
stable `code` and optional `path`. Validation `details` contain only field `location`,
error `type`, and a safe `message`, not raw input values or exception contexts;
unsupported versions use `unsupported_schema_version` rather than `invalid_snapshot`.
JSON failures use `invalid_json` with `line`/`column` for syntax errors or
`invalid_encoding` for non-UTF-8 files; filesystem failures use `snapshot_io_error`.
The original exception is retained as `__cause__` for debugging and may contain raw
data: do not expose its traceback or diagnostics to untrusted callers. The CLI renders
only safe public diagnostics and maps error categories to the exit codes below.

### CLI commands

#### Implemented offline workflow

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
See [raw-source ingestion](docs/raw-source-ingestion.md) for the full flag matrix,
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

#### Planned command extensions

Beyond the implemented offline workflow and live `collect` command below, these extensions remain planned:

| Command | Purpose |
| --- | --- |
| `query` | Run deterministic exposure, database-access, security-group-impact, trust, and ownership queries. |
| `ask` | Ask a natural-language question against a selected snapshot. |
| `compare` | Compare selected snapshots and their coverage. |

Provide readable output and machine-readable JSON. CLI commands call public application services rather than containing discovery or analysis logic.

## Ingestion and analysis

### AWS discovery

Implemented collection uses boto3 named profiles or configured assume-role access, verifies
accounts through STS, and reads selected regional configuration and IAM once per account.
Pagination, bounded concurrency, retries, an explicit operation allowlist, and partial-result
retention apply. It retrieves configuration metadata, not workload data or secret values.

ALB/NLB listeners/targets, ECS services/tasks and referenced task definitions, Lambda configuration,
RDS/Aurora instances/clusters/subnet groups, and SQS configuration/policies are implemented.
The service-local mappings describe collected facts and deliberately excluded data.

### AWS request model and allowlist

`AwsCollectionRequest` holds one `AwsAccountConfig` per account (unique expected account IDs; one
snapshot per account). Each config names a profile, an expected 12-digit account ID, concrete
unique regions or enabled-region discovery, selected services or `all_services=True`, optional
`bootstrap_region` / `role_arn` / `external_id` / `session_name`, concurrency/call limits, connect and
read timeouts, and `max_attempts`. Explicit IAM-only scope needs no regions. Identity is verified with STS `GetCallerIdentity` on the final
(assumed-role) session before any resource call; a mismatch or failure raises `IngestionError`
(`account_mismatch`, `identity_failed`) and produces no snapshot. Trust principals are never
followed into other accounts.

Only allowlisted operations can be called (`ALLOWED_OPERATIONS` in `archdiscov.ingestion.aws`):
`sts:GetCallerIdentity`, `sts:AssumeRole` (configured roles only), `ec2:DescribeRegions`
(enabled-region discovery), and the nine EC2 inventory read operations
in `Ec2Operation` (steps 12–13), plus the eight IAM reads in `IamOperation` (step 14) and six
ELB reads in `Elbv2Operation` (step 15), and the configuration reads in `EcsOperation`,
`LambdaOperation`, `RdsOperation`, and `SqsOperation` (step 16). Exact permissions are
published in [the consolidated policy and setup guide](docs/aws-permissions.md), with
resource-scoping constraints, profile/SSO/assume-role examples, and runtime credential
mounts. The policy is checked against the executable allowlist in normal tests.
Other operations raise `operation_not_allowed`
before any client is created. `OperationRunner` paginates, keeps pages gathered before a failure
(`incomplete`), reports first-call failures as `failed`, supports `skipped`/`unsupported`, bounds
concurrency, and emits sanitized diagnostics with no raw exception text. The shared
`DetailRunner` additionally aggregates bounded parameterized reads and handles each
service's pagination convention, retaining successful sibling results after failures.

### AWS configuration collection and the `collect` command

`archdiscov collect` is the first live workflow. It is read-only and collects regional EC2
VPCs, subnets, instances, and network configuration (route tables, internet gateways, NACLs,
security groups and rules, network interfaces), plus account-wide IAM roles, instance profiles,
inline policies, and referenced managed policies' default versions. Regional ELBv2 collection
adds ALB/NLB load balancers, listeners, ALB rules, target groups, registrations/health, and tags.
Snapshots are schema `"2"`; attachments and trust references use evidence-backed `attached_to`
dependencies, while observed forwarding uses `forwards_to`. Neither proves granted access or
end-to-end reachability. Mappings, permissions, coverage, redaction, and limitations are in
[docs/network-mapping.md](docs/network-mapping.md) and
[docs/iam-mapping.md](docs/iam-mapping.md), plus
[docs/load-balancing-mapping.md](docs/load-balancing-mapping.md). The CLI accepts `ec2`, `iam`,
`elasticloadbalancing`, `ecs`, `lambda`, `rds`, and `sqs`.
Gateway Load Balancers are explicit unsupported summaries; classic ELB is not collected.

```bash
# Default service is EC2 with explicit region selection.
archdiscov collect --account dev:123456789012 --region us-east-1 --region eu-west-1 \
  --output dev-ec2.json --json
# All seven supported families; discover enabled regions by default.
archdiscov collect --account dev:123456789012 --all-services --output dev-all.json
# Explicit regions override default discovery in all-services mode.
archdiscov collect --account dev:123456789012 --all-services --region us-east-1 \
  --output dev-all-us-east-1.json
# Selective collection can opt into enabled-region discovery too.
archdiscov collect --account dev:123456789012 --service lambda --region-scope enabled \
  --bootstrap-region us-east-1 --output dev-lambda.json
# IAM-only needs no --region.
archdiscov collect --account dev:123456789012 --service iam --output dev-iam.json
# ELB-only requires a concrete region; it does not implicitly collect EC2 or IAM.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service elasticloadbalancing --output dev-elbv2.json
# --service is repeatable; combined regional and account-wide collection.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service ec2 --service iam --service elasticloadbalancing --output dev-combined.json
# Configuration-only service slices can be selected independently or combined.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service ecs --service lambda --service rds --service sqs --output dev-config.json
# Several accounts: repeat --account PROFILE:ACCOUNT_ID[:ROLE_ARN] and give one --output per account, in order.
```

Without `--all-services`, omitting `--service` preserves the EC2 default; explicit services
collect only those selections. `--all-services` conflicts with any explicit `--service`.
It defaults to enabled-region discovery unless repeatable `--region` values select explicit
scope. Selective collection retains explicit scope by default; use `--region-scope enabled`
to discover regions instead. Enabled scope conflicts with any `--region`; explicit regional
collection requires concrete regions, never `global`. IAM-only explicit scope needs none. IAM runs
once per account even when multiple regions are supplied; IAM-only snapshots can have an empty
`regions` mapping with `coverage.account_wide: true`. IAM resources use AWS source IDs in
`global_resources`, including referenced AWS-managed policies scoped to the collection account.

Each account yields its own snapshot file. Outputs must be distinct and are never overwritten
(an existing path is a conflict for that account only). Identity is verified through STS first; a
mismatch produces no snapshot for that account. Failures in one account do not stop the others.

Discovery runs after STS verification and requires `ec2:DescribeRegions` on `"*"`, even
without EC2 inventory. `--bootstrap-region` selects a partition-appropriate endpoint,
not inventory scope. Only supported enabled regions are collected; discovery/bootstrap
failures or unsupported regions fail that account safely rather than silently shrinking
scope. Missing SDK service endpoints produce partial `unsupported` coverage, not empty success.
Supply credentials at runtime through profiles/assume-role configuration, never snapshot files.

The CLI exposes run/account SDK-call budgets, active-account and collector-job limits,
and local runner concurrency; defaults and maxima are listed in
[scope, discovery, and limits](docs/collection-scope.md#limits). Broader scope increases API
traffic, runtime, throttling risk, and possible AWS costs; limits bound concurrency, not
total calls or cost. All-services means seven supported families, not every AWS service,
and collection is not a transactionally consistent point-in-time view.

Readable results include scope lines for services, resolved regions, region mode, and
account-wide IAM. JSON account results expose `collection_scope` from snapshot metadata;
scope is not proof of completeness. Inspect operation coverage and `PARTIAL` outcomes.
Independent collector jobs feed deterministic account assembly and relationship resolution.
Public async collection/persistence APIs and synchronous wrappers are implemented;
cancellation drains running work. Started saves may commit during cancellation, and
committed files remain intact.

- **Containment:** VPC → subnet → instance from observed `VpcId`/`SubnetId`. When a parent is
  not in the collected results, the resource is stored once at the regional root with an
  `attributes.unresolved_parent` explanation; parents are never invented. ELBv2 adds VPC →
  load balancer → listener → rule and VPC → target group, with subnet/security-group attachments
  rather than duplicated resources. ELB-only VPC parents and missing same-scope targets stay
  unresolved; IP ownership is not inferred, and Lambda registrations are external references.
- **Evidence:** every resource cites its whole API item (e.g. `Reservations[0].Instances[0]`);
  facts such as CIDRs, availability zone, instance type, state, and tags cite the exact source
  field. Tag values whose key names a secret are redacted and recorded in `redacted_paths`.
- **Coverage:** EC2 has one record per region and operation; IAM has eight account-wide
  aggregates, and ELBv2 six regional operation aggregates across requests/pages.
  Counts are retained source records, not normalized
  resources. Zero results are `success` with count `0` only with complete prerequisites;
  later-page failures retain earlier pages as `incomplete`. IAM/ELBv2 downstream reads preserve
  known results but mark incomplete discovery, or are `skipped` when no requests can be discovered.
  Missing tag replies are not empty tag maps; empty target-health replies in partial batches
  remain unavailable rather than proving no registrations. Target health is not reachability.
- **Exit codes:** `0` success (the summary says `PARTIAL` when any operation did not succeed),
  `1` filesystem error, `2` invalid request/validation, `3` destination exists, `5` identity or
  other ingestion failure. Required read permissions are listed in
  [docs/network-mapping.md](docs/network-mapping.md), [docs/iam-mapping.md](docs/iam-mapping.md),
  and [docs/load-balancing-mapping.md](docs/load-balancing-mapping.md).

#### Compute, database, and queue configuration

Service-local contracts, exact operations/permissions, source paths, and limitations:

| Family | Collected facts | Explicit limits |
| --- | --- | --- |
| [EC2/network](docs/network-mapping.md) | VPC/subnet/instance configuration, routes, rules, ENIs, attachments | No NAT/transit/peering inventory or reachability analysis; nonlocal/missing targets remain explicit |
| [IAM](docs/iam-mapping.md) | Roles, profiles, trust/inline policies, referenced managed policies | Default policy versions only; no users/groups inventory or effective-permission evaluation |
| [ALB/NLB](docs/load-balancing-mapping.md) | Listeners, rules, ordered forwarding, registrations and reported health | No classic ELB; Gateway LB summaries only; health is not reachability and IP ownership is not inferred |
| [ECS](docs/ecs-mapping.md) | Clusters, services, desired-running tasks, referenced task definitions | No stopped-task inventory, workload commands, or environment values |
| [Lambda](docs/lambda-mapping.md) | Function configuration, networking, execution-role references | No function code or environment values |
| [RDS/Aurora](docs/rds-mapping.md) | Instances, clusters, subnet groups, endpoints/ports, networking | No database contents or credentials; configured public flags are not exposure conclusions |
| [SQS](docs/sqs-mapping.md) | Queue configuration, sanitized policies, dead-letter references | No messages or effective-access conclusions |

Each selected service has regional aggregate coverage with retained partial results and
source-owned fact citations. References resolve after all selected inventories are collected;
missing same-scope targets stay unresolved, while known nonlocal ARNs stay external.
Collection does not retrieve queue messages, function code, database contents, workload
commands, or environment-variable values. Role/policy references describe configuration,
not effective access. Schema `1` snapshots remain readable; new output uses schema `2`.

### Infrastructure as code

#### Terraform state API (implemented)

Import a local **public `terraform show -json` state export**, not a raw internal state
file, backend, or plan. Format major version `1` is supported, including future minor
versions. The importer never executes Terraform, loads providers, or makes network calls.
State is exported configuration, not a fresh observation of AWS.

From the repository root, run this Python example with `.venv/bin/python` after installation:

```python
from archdiscov.ingestion.terraform import TerraformStateRequest
from archdiscov.services import collect_snapshots, get_evidence, save_snapshot

request = TerraformStateRequest(
    path="tests/fixtures/terraform/state.json",
    account_id="123456789012",
    region="eu-west-1",
)
snapshot = collect_snapshots(request)[0]
save_snapshot(snapshot, "snapshots/terraform-state-example.json")
evidence_id = next(iter(snapshot.evidence))
print(get_evidence(snapshot, evidence_id))
```

Use a new output path with an existing parent directory; saving never overwrites.
`collect_snapshots(request, context=...)` returns a list of snapshots, with optional
injected context; `collect_snapshots_async` is also available. Account and region can
be omitted only when source facts resolve their scope unambiguously. The fixture uses
the explicit account and region above because not every resource carries scope facts.

Supported typed resources include VPCs, subnets, instances, security groups, internet
gateways, route tables, ENIs, and IAM roles/profiles/policies. Only allowlisted facts and
exported VPC/subnet/security-group references are mapped. Sensitive values are redacted;
unsupported types remain summaries with coverage limitations. See the
[Terraform state mapping](docs/terraform-state-mapping.md) for exact fields, evidence,
scope rules, and omitted rules/routes/policy documents.

#### Terraform plan API (implemented)

Import a local **public `terraform show -json` plan export**, format major `1`, using
`TerraformPlanRequest`. This separate adapter accepts neither state exports/internal
state nor HCL, and never executes Terraform, loads providers, or makes network calls.
From the repository root, run this example with `.venv/bin/python` after installation:

```python
from archdiscov.ingestion.terraform import TerraformPlanRequest
from archdiscov.services import collect_snapshots, get_evidence, save_snapshot

request = TerraformPlanRequest(
    path="tests/fixtures/terraform/plan.json",
    account_id="123456789012",
    region="eu-west-1",
)
snapshot = collect_snapshots(request)[0]
save_snapshot(snapshot, "snapshots/terraform-plan-example.json")
evidence_id = next(iter(snapshot.evidence))
print(get_evidence(snapshot, evidence_id))
```

`collect_snapshots_async` supports the same request; optional `context` injection and
immutable saving follow the state API. Output uses schema `2` and `terraform_plan`.
Create/read/update/no-op/replacement resources use sanitized `after` facts; pure
deletions retain `before` facts with separate containment. Ordered `lifecycle.actions`
and `lifecycle.value_scope` distinguish these views, not deployed observations.

Unknown planned facts are omitted, never filled from old values. Sensitive masks and
generic secret-key scrubbing run before mapping and evidence. The adapter inherits
the state fact allowlist and complex-mapping limitations; uniquely matched exported
configuration references can resolve locally, but expressions are not evaluated.
See [Terraform plan mapping](docs/terraform-plan-mapping.md) for exact lifecycle,
scope, unknown-value evidence, and unresolved-reference rules. Schema `1` historical
snapshots remain readable; historical action-only plan snapshots remain valid.

#### CloudFormation API (implemented)

Import JSON/YAML declarations without deploying stacks or fetching external templates:

```python
from archdiscov.ingestion.cloudformation import CloudFormationRequest
from archdiscov.services import collect_snapshots, save_snapshot

request = CloudFormationRequest(
    path="tests/fixtures/cloudformation/template.yaml",
    account_id="123456789012",
    region="eu-west-1",
    stack_name="demo",
    parameters={"Stage": "test"},
)
snapshot = collect_snapshots(request)[0]
save_snapshot(snapshot, "snapshots/cloudformation-example.json")
```

Use `.venv/bin/python` after installation and a fresh output path. The async service
accepts the same request. Logical-only identities, locally evaluated intrinsics,
NoEcho/sensitive-path redaction, source-owned references, and qualified coverage
survive immutable persistence. Missing physical values, nested-stack contents, external
imports, unsupported intrinsics and mappings remain explicit limitations. See
[CloudFormation mapping](docs/cloudformation-mapping.md) for the supported subset,
exact facts, safety limits, and evidence behavior.

#### IaC delivery status
- Raw-source CLI ingestion is implemented (step 21); see [formats, flags, and service contracts](docs/raw-source-ingestion.md).
- Phase-2 acceptance and closure remains planned (step 22).
- No Terraform execution, infrastructure apply, stack deployment, or arbitrary template code evaluation.

### Deterministic findings (planned)

| Analysis | Behavior and limits |
| --- | --- |
| Internet exposure | Trace supported public ingress through addressing, routes, security groups, NACLs, listeners, and targets. Report candidate exposure and blockers; unresolved path elements produce an uncertain result. |
| Database access | Identify compute resources with a supported potential network path to the selected database and port. Distinguish network permission from successful database authentication. |
| Security-group impact | List direct attachments and rule references, then trace downstream dependencies with paths and configurable depth. Label indirect impact as potential. |
| Cross-account trust | Extract role trust principals and supported resource-policy relationships, retaining conditions and distinguishing named accounts from wildcard or unresolved principals. Trust permission alone does not prove effective access. |
| Ownership | Report resources missing configured ownership tags. Default to `Owner` or `Team`; this is an ownership signal, not proof that nobody owns the resource. |

Do not attempt comprehensive IAM policy evaluation, organization-policy evaluation, application behavior analysis, or complete network simulation in the first release.

## Assistant and evidence contract

### Minimal result models (implemented)

`Finding` and `Answer` in `archdiscov.models.results` are separate result contracts,
not new fields in snapshot schema `1`. Both require an explicit `snapshot_id` UUID and
`status` (`AnalysisStatus` from `archdiscov.models.enums`), and retain nonblank
`evidence_ids` and `limitations` lists. Evidence IDs are keys in that snapshot's evidence
map, not globally unique identifiers.

| Contract | Additional fields | Citation requirements |
| --- | --- | --- |
| `Finding` | Nonblank `id`, nonblank `summary`, optional `resource_ids` list | `supported` and `partial` require at least one evidence ID |
| `Answer` | `finding_ids` list | `supported` and `partial` require at least one finding ID; direct evidence IDs are optional because findings carry citations |

Finding IDs are scoped to the selected snapshot's analysis results; resource IDs use the
existing stable identity contract. An answer references findings rather than embedding
provider-authored factual prose. A future renderer will obtain conclusions from validated
findings. These legacy contracts remain unchanged. Separate analysis contracts now
describe analyzer kinds, typed parameters, paths and structured limitations; there are
no analyzer execution, severity scores, provider messages, or query/ask commands yet.

| Status | Meaning and required limitations |
| --- | --- |
| `supported` | The stated conclusion is supported within its scope; scope caveats may still be recorded |
| `partial` | Some supported conclusions are available, but a nonempty limitations list must explain the gaps |
| `insufficient_evidence` | No definitive conclusion is justified; a nonempty limitations list explains why, and citations may be empty |

For example, represent the recorded ingress fact without claiming end-to-end internet access:

```python
from archdiscov.models.results import Answer, Finding
from archdiscov.services import load_snapshot

snapshot = load_snapshot("tests/fixtures/snapshots/partial-coverage.json")
finding = Finding(
    id="recorded-ingress",
    snapshot_id=snapshot.snapshot_id,
    status="partial",
    summary="The recorded ingress rule permits TCP port 443 from 0.0.0.0/0.",
    resource_ids=[snapshot.evidence["sg-rule"].resource_id],
    evidence_ids=["sg-rule"],
    limitations=["Route-table collection failed; end-to-end reachability is not established."],
)
answer = Answer(
    snapshot_id=snapshot.snapshot_id,
    status="partial",
    finding_ids=[finding.id],
    evidence_ids=finding.evidence_ids,
    limitations=finding.limitations,
)
assert Answer.model_validate_json(answer.model_dump_json()) == answer
```

These models validate **shape and status consistency only**: construction does not prove
that IDs resolve, that a conclusion is true, that coverage is sufficient, or that citations
were returned by tools. Analyzers must preserve relevant evidence/coverage limitations;
later answer validation must check the selected snapshot, findings, resource/evidence
references, and the tool-returned allowlist, and reject unsupported claims. Results remain
mutable and are not persisted by the snapshot service. Free-form summaries and limitations
must not contain secrets; these are not automatically sanitized or treated as instructions.

### Analysis result contracts (implemented)

`AnalysisResult` and `AnalysisFinding` in `archdiscov.models.analysis_results`
define a separate versioned result document with deterministic finding IDs,
cloud-aware references, ordered cited paths, typed analyzer parameters, and
structured coverage/unresolved-target limitations. Validate results against a selected
snapshot with `validate_analysis_result`; model construction alone checks only shape.
See [analysis result contracts](docs/analysis-results.md) for the API, ID rules,
fixtures, and integrity boundary. [Graph resource lookup, containment, dependencies, and evidence/coverage](docs/graph-index.md)
are implemented; queries, analyzer execution, immutable result persistence,
and analysis CLI commands remain later steps.

### Planned assistant behavior

- Define a provider-neutral interface for messages, tool calls, results, and final answers.
- Ship interchangeable Bedrock Converse and OpenAI-compatible adapters, plus a fake provider for tests. Configure provider, model, endpoint, credentials, timeouts, and request limits independently.
- Support explicitly configured Responses or Chat Completions protocols for the compatible adapter; reject unsupported tool-calling capabilities clearly. Follow [official function-calling guidance](https://developers.openai.com/api/docs/guides/function-calling).
- Give the assistant only typed, bounded tools for resource search, resource inspection, relationship traversal, deterministic findings, coverage inspection, and evidence retrieval. It receives no AWS credentials, shell access, arbitrary code execution, or mutation tools.
- Have analyzers produce evidence-backed findings before the LLM explains them. Resource names, tags, and imported text are data rather than instructions.
- Represent answers as findings, evidence references, limitations, and a status: `supported`, `partial`, or `insufficient_evidence`.
- Validate that cited findings and evidence exist in the selected snapshot and were returned by tools. Render factual conclusions from validated findings; the LLM cannot introduce additional architecture conclusions.
- Allow one repair attempt for invalid answers, then return a deterministic explanation of the failure.
- Qualify answers when relevant coverage is incomplete. Refuse definitive conclusions when missing evidence prevents them. Display snapshot timestamps and avoid presenting planned architecture as deployed state.
- Exclude sensitive configuration fields during ingestion and send providers only bounded tool results.

The first release should answer these example questions:

- What receives traffic from the public internet?
- Which services can access this database?
- What would be affected if this security group changed?
- Where are the cross-account trust relationships?
- Which resources appear to have no clear owner?

Every factual conclusion must cite discovered resources or configuration evidence and state relevant limitations.

## Delivery and validation

| Increment | Current status |
| --- | --- |
| 1. Models, snapshots, fixtures, persistence, evidence inspection, and CLI foundation | Complete (steps 1–9) |
| 2. AWS and infrastructure-as-code ingestion | Complete (steps 10–22): supported AWS inventory, offline IaC imports, explicit CLI formats, permissions and offline acceptance |
| Pre-phase-3 cloud boundary bridge (22.1–22.4) | Implemented foundation: scopes/identities/resources, schema `3` and conversion, offline Azure workflows, and common access/compatibility documentation; not live Azure support |
| 3. Graph queries, deterministic findings, and snapshot comparisons | Result contracts/integrity implemented (step 23); execution and subsequent steps planned |
| 4. LLM-provider adapters, orchestration, validated answers, and evaluations | Planned; separate from cloud platforms |

The bridge prepares phase 3 to build common graph/query and ownership contracts.
Security, network, and trust analyzers are initially planned for AWS; unsupported Azure
analysis must return actionable limitations rather than reuse AWS semantics. No graph,
analyzer, GCP, Terraform HCL parser, Azure authentication, or live Azure collection is
implemented by this bridge. See [boundary validation and extension guidance](docs/cloud-snapshots.md#validation-and-future-extensions).

### Offline increment acceptance

The first increment is exercised by `tests/test_offline_acceptance.py`: from outside the
checkout, with credential variables removed, it imports hierarchy and partial-coverage
fixtures using the installed CLI, inspects cited evidence, confirms typed children and
coverage survive persistence, rejects invalid references with safe diagnostics, and
checks that re-importing cannot overwrite saved bytes.

Run the acceptance test directly and in the Docker development environment:

```bash
uv sync --locked --python 3.13
.venv/bin/python -m pytest tests/test_offline_acceptance.py -q
docker compose build dev
docker compose run --rm -T dev python -m pytest tests/test_offline_acceptance.py -q
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev python -m pytest -q
```

The last command runs the entire normal suite with networking disabled and no credential
mounts. Image builds/installation may need registry access; execution does not. Docker
tests use `/opt/venv`, never the bind-mounted host `.venv`.

For a manual container import and readable evidence inspection, use a fresh destination:

```bash
mkdir -p snapshots
docker compose run --rm -T dev archdiscov ingest \
  tests/fixtures/snapshots/partial-coverage.json --output snapshots/docker-example.json
docker compose run --rm -T dev archdiscov evidence sg-rule \
  --snapshot snapshots/docker-example.json
```

Choose another output path if it already exists. Saved files are written as the configured
non-root user. Installation, snapshot schema/source formats, fixture purposes, Python APIs,
and supported command/error behavior are documented above. AWS, Terraform, and CloudFormation adapters
and raw-source CLI ingestion use these contracts today; analysis and
providers remain later work.

### AWS collection acceptance

`tests/test_whole_architecture_acceptance.py` exercises populated multi-account,
multi-region collection through the CLI using injected AWS clients, without credentials
or network calls. It covers all seven families, IAM once per account, partial results,
scope discovery/overrides, evidence inspection, redaction, failure isolation, and no overwrite.
Runner and async workflow tests additionally cover pagination, bounded concurrency,
deterministic assembly, and cancellation. Run the collection journey with:

```bash
.venv/bin/python -m pytest tests/test_whole_architecture_acceptance.py -q
```

GitHub Actions runs the full offline suite and quality checks, not live account scans.
Real-AWS checks require explicit runtime credentials and remain opt-in.

### Phase 2 offline acceptance

The phase closes with complementary journeys rather than live-AWS requirements:

- `tests/test_phase_two_acceptance.py`: real Botocore Stubber requests through
  collection, validated persistence and exact evidence inspection; a denied read
  remains partial while successful zero inventory stays distinguishable. Explicit
  local/external/unresolved targets retain their identifiers, reasons and citations.
- `tests/test_whole_architecture_acceptance.py`: populated seven-family collection
  across accounts/regions, with discovery, isolated failures, redaction and immutable files.
- `tests/test_raw_ingestion_acceptance.py`: both installed entry points outside the
  checkout for every source format, context/format failures, protected parameters,
  partial summaries, evidence inspection and no overwrite.
- `tests/test_offline_acceptance.py` and the fixture persistence suite preserve
  historical schema `1`, typed children, citations and qualified coverage.
- `tests/test_permissions_documentation.py` prevents the published inventory policy
  from drifting from the executable allowlist, including the separate STS rules.

Run the full acceptance and regression suite without any credential mounts:

```bash
.venv/bin/python -m pytest -q
docker compose build dev
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev /opt/venv/bin/python -m pytest -q
```

The host uses `.venv/bin/python`; Docker uses `/opt/venv/bin/python` and no host
virtual environment. Installation/builds may need network access, test execution
does not. Do not mount AWS credentials for these checks. Live smoke collection is
opt-in: authenticate a runtime profile and deliberately choose scope and a fresh
output using [the setup guide](docs/aws-permissions.md). It is not part of pytest
or CI and is not needed to pass acceptance.

Completeness remains relative to selected supported inventory and operation coverage,
not all AWS services or a transactionally consistent view. IaC exports/declarations
are not deployed observations; complex mappings, unknown expressions and unfetched
nested templates stay explicit limitations in the linked format guides. Graph
queries, analyzers, comparisons and providers remain phases 3–4.

### Later acceptance scenarios

Use pytest, botocore Stubber, and fake-provider transcripts. Acceptance scenarios include:

- Public load balancer with a private backend; public IP with blocked ingress; missing routing evidence.
- Database access through security-group references, CIDRs, and denied rules, without claiming authentication.
- Direct and indirect security-group dependencies.
- Explicit and conditional cross-account trust.
- Missing ownership tags and configurable ownership keys.
- Terraform module addresses, planned deletions, unknown values, and CloudFormation unresolved references.
- Safe matching of observed and declared resources.
- Fabricated citations, unsupported claims, malicious imported text, provider errors, and tool-call limits.

Run the same question fixtures across configured providers and record answer status, finding IDs, citation validity, tool usage, latency, and available token usage. External-provider and real-AWS tests remain opt-in; normal tests require neither credentials nor network access.

Future increments will add supported IaC formats, analyzer limitations, and provider/evaluation instructions here as they become available.

## Assumptions and defaults

- Use Python 3.13, uv, boto3, Pydantic, Typer, PyYAML, and pytest.
- Accounts are explicitly configured; regions are selected explicitly or discovered after identity verification. AWS Organizations discovery is deferred.
- Terraform HCL parsing, additional AWS services, web UI, historical monitoring, and automatic remediation are deferred.
- Saved paths are immutable through the persistence API; loaded models remain mutable. Collection creates a new snapshot, and future answers must identify the snapshot they analyze.
