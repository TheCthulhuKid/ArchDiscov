"""Synthetic schema-2 AWS snapshot with every attaching service and rule-reference form."""

from copy import deepcopy
from typing import Any

from archdiscov.models.resource_base import ResourceIdentity

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
REGION = "eu-west-1"
SNAPSHOT_ID = "00000000-0000-4000-8000-0000000000a1"
ARNS = {
    "lb": f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/web/50dc6c495c0c9188",
    "ecs": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/cluster/web",
    "lambda": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:handler",
    "rds": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:orders",
}
SERVICES = ("ec2", "elasticloadbalancing", "ecs", "lambda", "rds")


def rid(service: str, physical_id: str) -> str:
    identity = ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=service, physical_id=physical_id
    )
    return identity.stable_id


IDS = {
    "vpc": rid("ec2", "vpc-main"),
    "subnet": rid("ec2", "subnet-app"),
    "sg-app": rid("ec2", "sg-app"),
    "sg-db": rid("ec2", "sg-db"),
    "sg-cache": rid("ec2", "sg-cache"),
    "sg-lonely": rid("ec2", "sg-lonely"),
    "instance": rid("ec2", "i-app"),
    "contained": rid("ec2", "i-contained"),
    "eni": rid("ec2", "eni-app"),
    "lb": rid("elasticloadbalancing", ARNS["lb"]),
    "ecs": rid("ecs", ARNS["ecs"]),
    "lambda": rid("lambda", ARNS["lambda"]),
    "rds": rid("rds", ARNS["rds"]),
}
EVIDENCE = {
    name: f"ev-{name}"
    for name in (
        "instance",
        "eni",
        "lb",
        "ecs",
        "lambda",
        "rds",
        "sg-app",
        "sg-db",
        "sg-cache",
        "subnet",
        "vpc",
    )
}


def _resource(name: str, service: str, resource_type: str, physical_id: str, **fields) -> dict:
    return {
        "identity": {
            "account_id": ACCOUNT,
            "region": REGION,
            "service": service,
            "physical_id": physical_id,
        },
        "resource_type": resource_type,
        "evidence_ids": [EVIDENCE[name]] if name in EVIDENCE else [],
        **fields,
    }


def _evidence(name: str) -> dict:
    return {
        "snapshot_id": SNAPSHOT_ID,
        "resource_id": IDS[name],
        "source": "mock",
        "locator": "security-group-data",
        "configuration_path": f"{name}.SecurityGroups",
        "value": {"name": name},
    }


def _membership(source: str) -> dict:
    return {
        "source_id": IDS[source],
        "target": {"kind": "local", "resource_id": IDS["sg-app"]},
        "relationship": "attached_to",
        "attributes": {"kind": "security_group_membership"},
        "evidence_ids": [EVIDENCE[source]],
    }


def _reference(source: str, target: dict, **attributes: Any) -> dict:
    return {
        "source_id": IDS[source],
        "target": target,
        "relationship": "attached_to",
        "attributes": {"kind": "security_group_reference", **attributes},
        "evidence_ids": [EVIDENCE[source]],
    }


def _dependency(source: str, target: dict, kind: str, **attributes: Any) -> dict:
    return {
        "source_id": IDS[source],
        "target": target,
        "relationship": "attached_to",
        "attributes": {"kind": kind, **attributes},
        "evidence_ids": [EVIDENCE[source]],
    }


def _local(name: str) -> dict:
    return {"kind": "local", "resource_id": IDS[name]}


def _operation(service: str, status: str = "success") -> dict:
    data = {
        "account_id": ACCOUNT,
        "region": REGION,
        "service": service,
        "operation": f"Describe{service.capitalize()}",
        "status": status,
        "started_at": "2026-10-13T08:00:00Z",
        "completed_at": "2026-10-13T08:00:10Z",
    }
    if status == "success":
        data["result_count"] = 1
    elif status == "failed":
        data["failures"] = [{"code": "AccessDenied", "message": "Denied"}]
    else:
        data["reason"] = "Not collected"
    return data


def snapshot_data(*, services: tuple[str, ...] = SERVICES, statuses: dict | None = None) -> dict:
    """Build the snapshot; `services` limits recorded coverage, `statuses` overrides outcomes."""
    statuses = statuses or {}
    resources = [
        _resource(
            "vpc",
            "ec2",
            "AWS::EC2::VPC",
            "vpc-main",
            cidr_blocks=["10.0.0.0/16"],
            children=[
                _resource(
                    "subnet",
                    "ec2",
                    "AWS::EC2::Subnet",
                    "subnet-app",
                    children=[
                        _resource("instance", "ec2", "AWS::EC2::Instance", "i-app"),
                        _resource("contained", "ec2", "AWS::EC2::Instance", "i-contained"),
                        _resource("eni", "ec2", "AWS::EC2::NetworkInterface", "eni-app"),
                    ],
                ),
                _resource("sg-app", "ec2", "AWS::EC2::SecurityGroup", "sg-app"),
                _resource("sg-db", "ec2", "AWS::EC2::SecurityGroup", "sg-db"),
                _resource("sg-cache", "ec2", "AWS::EC2::SecurityGroup", "sg-cache"),
                _resource("sg-lonely", "ec2", "AWS::EC2::SecurityGroup", "sg-lonely"),
            ],
        ),
        _resource(
            "lb",
            "elasticloadbalancing",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            ARNS["lb"],
            arn=ARNS["lb"],
        ),
        _resource("ecs", "ecs", "AWS::ECS::Service", ARNS["ecs"], arn=ARNS["ecs"]),
        _resource("lambda", "lambda", "AWS::Lambda::Function", ARNS["lambda"], arn=ARNS["lambda"]),
        _resource("rds", "rds", "AWS::RDS::DBInstance", ARNS["rds"], arn=ARNS["rds"]),
    ]
    dependencies = [
        *(_membership(name) for name in ("instance", "eni", "lb", "ecs", "lambda", "rds")),
        {**_membership("lambda"), "target": _local("sg-db")},
        _reference("sg-app", _local("sg-db"), direction="egress", protocol="tcp", from_port=5432),
        _reference("sg-cache", _local("sg-app"), direction="ingress", protocol="tcp", from_port=80),
        _reference("sg-app", _local("sg-app"), direction="ingress", protocol="-1"),
        _reference(
            "sg-app",
            {
                "kind": "external",
                "identifier": "sg-partner",
                "account_id": OTHER_ACCOUNT,
                "service": "ec2",
                "aws_resource_type": "AWS::EC2::SecurityGroup",
            },
            direction="ingress",
            protocol="tcp",
            from_port=443,
        ),
        _reference(
            "sg-app",
            {"kind": "unresolved", "reason": "Group not collected", "source_path": "Rules[9]"},
            direction="egress",
        ),
        _reference(
            "sg-app",
            {"kind": "external", "identifier": "pl-123", "service": "ec2", "region": REGION},
            direction="egress",
            target_kind="prefix_list",
        ),
        # Downstream dependencies: a chain, a cycle, parallel edges, and a cross-account end.
        _dependency("instance", _local("subnet"), "subnet_membership"),
        _dependency("instance", _local("subnet"), "subnet_membership", note="secondary"),
        _dependency("subnet", _local("vpc"), "subnet_membership"),
        _dependency("vpc", _local("subnet"), "subnet_membership", note="cycle"),
        _dependency(
            "instance",
            {
                "kind": "external",
                "identifier": f"arn:aws:iam::{OTHER_ACCOUNT}:role/shared",
                "account_id": OTHER_ACCOUNT,
                "service": "iam",
                "aws_resource_type": "AWS::IAM::Role",
            },
            "instance_profile",
        ),
        # Recorded reference whose rule direction is unavailable.
        _reference("sg-db", _local("sg-app")),
    ]
    return {
        "schema_version": "2",
        "snapshot_id": SNAPSHOT_ID,
        "account_id": ACCOUNT,
        "collection_started_at": "2026-10-13T08:00:00Z",
        "collection_completed_at": "2026-10-13T08:01:00Z",
        "source": {"kind": "mock", "label": "security-group-data"},
        "coverage": {
            "regions": [REGION],
            "account_wide": False,
            "operations": [
                _operation(service, statuses.get(service, "success")) for service in services
            ],
        },
        "regions": {REGION: {"resources": resources}},
        "dependencies_by_region": {REGION: dependencies},
        "evidence": {EVIDENCE[name]: _evidence(name) for name in EVIDENCE},
    }


def variant(**options) -> dict:
    return deepcopy(snapshot_data(**options))


def schema_one_data(**options) -> dict:
    """The same snapshot as schema 1, whose dependencies can only be local `target_id`s."""
    data = variant(**options)
    data["schema_version"] = "1"
    edges = []
    for edge in data["dependencies_by_region"][REGION]:
        if edge["target"]["kind"] != "local":
            continue
        edges.append(
            {
                **{k: v for k, v in edge.items() if k != "target"},
                "target_id": edge["target"]["resource_id"],
            }
        )
    data["dependencies_by_region"][REGION] = edges
    return data
