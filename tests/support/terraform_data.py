"""Terraform exports, plans, and requests shared by the Terraform import tests."""

import json
from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.terraform import TerraformPlanRequest
from archdiscov.ingestion.terraform.catalog import service_for_type
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resource_base import ResourceIdentity
from archdiscov.services import collect_snapshots
from tests.paths import FIXTURES_ROOT
from tests.support.network_data import flatten

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = AdapterContext(clock=lambda: NOW, uuid_factory=lambda: UUID(int=18))
FIXTURE = FIXTURES_ROOT / "terraform" / "plan.json"
AWS_PROVIDER = "registry.terraform.io/hashicorp/aws"
ACCOUNT = "123456789012"


def change(address="aws_instance.example", actions=None, before=None, after=None, **extra):
    return {
        "address": address,
        "type": address.split(".")[-2].split("[")[0],
        "mode": "managed",
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {
            "actions": actions or ["create"],
            "before": before,
            "after": {} if after is None else after,
            **extra,
        },
    }


def write_plan(tmp_path, changes, **extra):
    path = tmp_path / "input-plan.json"
    path.write_text(
        json.dumps(
            {
                "format_version": "1.2",
                "planned_values": {"root_module": {}},
                "resource_changes": changes,
                **extra,
            }
        )
    )
    return path


def collect(path):
    return collect_snapshots(plan_request(path), context=CONTEXT)[0]


def resources(snapshot):
    return {
        resource.identity.logical_address: resource
        for tree in snapshot.regions.values()
        for resource in flatten(tree.resources)
    }


def resource(address="aws_vpc.main", **overrides):
    result = {
        "address": address,
        "mode": "managed",
        "type": "aws_vpc",
        "provider_name": AWS_PROVIDER,
        "values": {"id": "vpc-123", "tags": {"Name": "main"}},
        "sensitive_values": {},
    }
    result.update(overrides)
    return result


def identity(kind="aws_vpc", physical_id="source-id"):
    return ResourceIdentity(
        account_id="123456789012",
        region=AwsRegion.GLOBAL
        if service_for_type(kind) == AwsService.IAM
        else AwsRegion.US_EAST_1,
        service=service_for_type(kind),
        physical_id=physical_id,
        logical_address=f"{kind}.example",
    )


def plan_request(path, **extra):
    return TerraformPlanRequest(path=path, account_id=ACCOUNT, region="eu-west-1", **extra)
