# Infrastructure-as-code import

Importing Terraform state and plan exports and CloudFormation templates through the Python
services. The CLI form of the same imports is in [raw-source ingestion](raw-source-ingestion.md).

## Terraform state API

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
[Terraform state mapping](terraform-state-mapping.md) for exact fields, evidence,
scope rules, and omitted rules/routes/policy documents.

## Terraform plan API

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
See [Terraform plan mapping](terraform-plan-mapping.md) for exact lifecycle,
scope, unknown-value evidence, and unresolved-reference rules. Schema `1` historical
snapshots remain readable; historical action-only plan snapshots remain valid.

## CloudFormation API

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
[CloudFormation mapping](cloudformation-mapping.md) for the supported subset,
exact facts, safety limits, and evidence behavior.

## IaC delivery status

- Raw-source CLI ingestion is implemented (step 21); see [formats, flags, and service contracts](raw-source-ingestion.md).
- Phase-2 acceptance and closure are implemented; see [Phase 2 offline acceptance](acceptance.md#phase-2-offline-acceptance).
- No Terraform execution, infrastructure apply, stack deployment, or arbitrary template code evaluation.
