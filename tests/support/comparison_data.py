"""Small snapshots to compare, built from plain data through snapshot validation, with cited
AWS resources and evidence."""

import copy
import json
from uuid import UUID

from archdiscov.graph import GraphIndex
from archdiscov.services import upgrade_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT

ACCOUNT = "123456789012"
REGION = "eu-west-1"


def aws_identity(physical=None, logical=None, *, account=ACCOUNT, region=REGION, service="ec2"):
    identity = {"account_id": account, "region": region, "service": service}
    if physical:
        identity["physical_id"] = physical
    if logical:
        identity["logical_address"] = logical
    return identity


def aws_resource(resource_type="AWS::EC2::VPC", name=None, **identity):
    data = {"identity": aws_identity(**identity), "resource_type": resource_type}
    if name:
        data["name"] = name
    return data


SOURCES = {
    "aws": {"kind": "aws", "profile": "test"},
    "mock": {"kind": "mock", "label": "test"},
    "terraform_state": {
        "kind": "terraform_state",
        "path": "state.json",
        "format_version": "1.0",
    },
    "terraform_plan": {
        "kind": "terraform_plan",
        "path": "plan.json",
        "format_version": "1.2",
    },
    "cloudformation": {"kind": "cloudformation", "path": "t.json", "stack_name": "s"},
}


def coverage(status="success", *, service="ec2", operation="DescribeVpcs", region=REGION):
    """A recorded operation outcome of the test account."""
    data = {
        "account_id": ACCOUNT,
        "region": region,
        "service": service,
        "operation": operation,
        "status": status,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:30Z",
    }
    if status in ("success", "incomplete"):
        data["result_count"] = 1
    if status == "incomplete":
        data["reason"] = "partial"
    if status == "failed":
        data["failures"] = [{"code": "denied", "message": "access denied"}]
    if status in ("skipped", "unsupported"):
        data["reason"] = "not collected"
    return data


def aws_snapshot(
    resources,
    source="aws",
    *,
    seed=1,
    account=ACCOUNT,
    region=REGION,
    dependencies=(),
    evidence=None,
    operations=(),
):
    data = {
        "schema_version": "2",
        "snapshot_id": str(UUID(int=seed)),
        "account_id": account,
        "collection_started_at": "2026-01-01T00:00:00Z",
        "collection_completed_at": "2026-01-01T00:01:00Z",
        "source": copy.deepcopy(SOURCES[source]),
        "coverage": {
            "regions": [region],
            "account_wide": False,
            "operations": copy.deepcopy(list(operations)),
        },
        "regions": {region: {"resources": resources}},
        "dependencies_by_region": {region: list(dependencies)} if dependencies else {},
        "evidence": copy.deepcopy(evidence or {}),
    }
    return validate_snapshot(data)


def index(resources, source="aws", **options):
    return GraphIndex(aws_snapshot(resources, source, **options))


def azure_snapshot(*, upper=False):
    data = json.loads((FIXTURES_ROOT / "snapshots" / "azure.json").read_text())
    if upper:
        _upper(data)
    return validate_snapshot(data)


def _upper(data):
    """The same Azure resources with differently cased ARM IDs, types, groups, and locations."""

    def fix(resource):
        identity = resource["identity"]
        identity["physical_id"] = (
            identity["physical_id"].upper().replace("/SUBSCRIPTIONS/", "/subscriptions/")
        )
        identity["resource_group"] = identity["resource_group"].upper()
        identity["native_type"] = identity["native_type"].lower()
        for child in resource.get("children", []):
            fix(child)

    for region in data["regions"].values():
        for resource in region["resources"]:
            fix(resource)


def rid(name, kind="physical"):
    return f"{ACCOUNT}/{REGION}/ec2/{kind}/{name}"


def vpc(physical="vpc-1", *, attributes=None, evidence=(), fact_evidence=None, children=(), **kw):
    data = aws_resource(physical=physical, **kw)
    if attributes is not None:
        data["attributes"] = attributes
    data["evidence_ids"] = list(evidence)
    data["fact_evidence"] = fact_evidence or {}
    data["children"] = list(children)
    return data


def evidence(seed, resource, path, *, status="present", value=None):
    data = {
        "snapshot_id": str(UUID(int=seed)),
        "resource_id": rid(resource),
        "source": "aws",
        "locator": "test",
        "configuration_path": path,
    }
    if status == "present":
        return {**data, "value": value}
    return {**data, "value_status": status, "limitation": "not available"}


def journey_snapshot(source, value, *, seed, schema="2", **options):
    resource, cited = cited_vpc("vpc-1", value, seed=seed, **options.pop("resource", {}))
    snapshot = aws_snapshot([resource], source, seed=seed, evidence=cited, **options)
    if schema == "3":
        return upgrade_snapshot(snapshot)
    if schema == "1":
        return validate_snapshot({**snapshot.model_dump(mode="json"), "schema_version": "1"})
    return snapshot


def cited_vpc(physical, value, *, seed, **extra):
    """A VPC whose `a` attribute is cited, so a difference has evidence on its side."""
    resource = {
        **aws_resource(physical=physical, **extra.pop("identity", {})),
        "attributes": {"a": value},
        "evidence_ids": ["cite"],
        "fact_evidence": {"/attributes/a": ["cite"]},
        **extra,
    }
    return resource, {"cite": evidence(seed, physical, "a")}
