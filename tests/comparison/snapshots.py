"""Small snapshots for matching tests, built from plain data through snapshot validation."""

import copy
import json
from uuid import UUID

from archdiscov.graph import GraphIndex
from archdiscov.services import validate_snapshot
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


def aws_snapshot(
    resources,
    source="aws",
    *,
    seed=1,
    account=ACCOUNT,
    region=REGION,
    dependencies=(),
    evidence=None,
):
    data = {
        "schema_version": "2",
        "snapshot_id": str(UUID(int=seed)),
        "account_id": account,
        "collection_started_at": "2026-01-01T00:00:00Z",
        "collection_completed_at": "2026-01-01T00:01:00Z",
        "source": copy.deepcopy(SOURCES[source]),
        "coverage": {"regions": [region], "account_wide": False},
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
