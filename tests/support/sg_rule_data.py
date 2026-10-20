"""Snapshots of security groups with recorded, cited rules for rule-matching tests."""

from uuid import UUID

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    RegionArchitecture,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resource_base import ResourceIdentity
from archdiscov.models.resources.network import SecurityGroup, SecurityGroupRule
from tests.support.coverage_data import ec2_coverage
from tests.support.iam_data import ACCOUNT, context

SNAPSHOT = UUID(int=292)
REGION = AwsRegion.EU_WEST_1


def group_id(name: str) -> str:
    return ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.EC2, physical_id=name
    ).stable_id


def rule(direction="ingress", protocol="tcp", from_port=443, to_port=443, **fields):
    return SecurityGroupRule(
        direction=direction,
        protocol=protocol,
        from_port=from_port,
        to_port=to_port,
        **fields,
    )


def build(
    groups: dict[str, list[SecurityGroupRule]],
    *,
    status: str | None = "success",
    uncited: bool = False,
    bare: tuple[str, ...] = (),
    omitted: tuple[str, ...] = (),
):
    """Groups by physical ID to their rules.

    `uncited` records rules without per-rule citations; `omitted` names groups whose
    invalid rules normalization dropped; `bare` names groups recorded with
    no evidence at all. A `status` of None records no EC2 coverage.
    """
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, REGION)
    resources = []
    for name, rules in groups.items():
        ingress = [r for r in rules if r.direction == "ingress"]
        egress = [r for r in rules if r.direction == "egress"]
        if name in bare:
            resources.append(
                SecurityGroup(
                    identity=ResourceIdentity(
                        account_id=ACCOUNT,
                        region=REGION,
                        service=AwsService.EC2,
                        physical_id=name,
                    ),
                    ingress_rules=ingress,
                    egress_rules=egress,
                )
            )
            continue
        facts = {}
        if not uncited:
            facts = {
                **{f"/ingress_rules/{n}": (f"IpPermissions[{n}]", {}) for n in range(len(ingress))},
                **{
                    f"/egress_rules/{n}": (f"IpPermissionsEgress[{n}]", {})
                    for n in range(len(egress))
                },
            }
        resources.append(
            builder._build(
                SecurityGroup,
                operation="DescribeSecurityGroups",
                item_path=f"SecurityGroups[{name}]",
                item={"GroupId": name},
                physical_id=name,
                attributes={"limitations": ["rule omitted"]} if name in omitted else {},
                facts=facts,
                ingress_rules=ingress,
                egress_rules=egress,
            )
        )
    operations = [ec2_coverage("DescribeSecurityGroups", status, len(resources))] if status else []
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        regions={REGION: RegionArchitecture(resources=resources)},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False, operations=operations),
        evidence=builder.evidence,
    )
