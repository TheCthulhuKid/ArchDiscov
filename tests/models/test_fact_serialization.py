import cProfile
import json
import pstats

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    Ec2Instance,
    Evidence,
    Resource,
    ResourceIdentity,
    Subnet,
    Vpc,
)
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resource_tree import walk_resources
from archdiscov.models.resources.queues import SqsQueue
from tests.paths import FIXTURES_ROOT


def resource_tree(depth: int, width: int) -> Vpc:
    counter = iter(range(100_000))

    def build(level: int) -> Resource:
        cls = Vpc if level == 0 else Subnet if level < depth - 1 else Ec2Instance
        resource = cls(
            identity=ResourceIdentity(
                account_id="123456789012",
                region=AwsRegion.EU_WEST_1,
                service=AwsService.EC2,
                physical_id=f"resource-{next(counter)}",
            ),
            attributes={"configuration": {"ports": [80, 443], "enabled": True}},
        )
        if level < depth - 1:
            resource.children = [build(level + 1) for _ in range(width)]
        return resource

    return build(0)


def snapshot_with_tree(depth: int = 3, width: int = 2) -> ArchitectureSnapshot:
    path = FIXTURES_ROOT / "snapshots" / "aws.json"
    snapshot = ArchitectureSnapshot.model_validate(json.loads(path.read_text()))
    snapshot.global_resources = []
    snapshot.regions[AwsRegion.EU_WEST_1].resources = [resource_tree(depth, width)]
    return snapshot


def cite(snapshot: ArchitectureSnapshot, resource: Resource, *pointers: str) -> None:
    snapshot.evidence[resource.id] = Evidence(
        snapshot_id=snapshot.snapshot_id,
        resource_id=resource.id,
        source="aws",
        locator="DescribeResources",
        configuration_path="Resources[0]",
        value=None,
    )
    resource.fact_evidence = {pointer: [resource.id] for pointer in pointers}


def test_nested_child_pointers_use_full_json_representation() -> None:
    snapshot = snapshot_with_tree()
    root = snapshot.regions[AwsRegion.EU_WEST_1].resources[0]
    leaf = root.children[0].children[0]
    cite(snapshot, root, "/children/0/children/0/instance_type")
    cite(snapshot, leaf, "/instance_type")
    snapshot.validate_snapshot_integrity()
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_invalid_nested_child_pointers_are_rejected() -> None:
    reason = "missing key"
    snapshot = snapshot_with_tree()
    root = snapshot.regions[AwsRegion.EU_WEST_1].resources[0]
    cite(snapshot, root, "/children/0/children/0/lifecycle")
    with pytest.raises(ValueError, match=reason):
        snapshot.validate_snapshot_integrity()
    with pytest.raises(ValidationError, match=reason):
        ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())


def test_cached_child_representation_honors_model_serializer() -> None:
    snapshot = snapshot_with_tree()
    root = snapshot.regions[AwsRegion.EU_WEST_1].resources[0]
    arn = "arn:aws:sqs:eu-west-1:123456789012:queue"
    queue = SqsQueue(
        identity=ResourceIdentity(
            account_id=snapshot.account_id,
            region=AwsRegion.EU_WEST_1,
            service=AwsService.SQS,
            physical_id=arn,
        ),
        arn=arn,
        queue_url="https://sqs.eu-west-1.amazonaws.com/123456789012/queue",
    )
    root.children = [queue]
    queue.redrive_allow_policy = {"redrivePermission": "denyAll", "extra": "not serialized"}
    cite(snapshot, root, "/children/0/queue_url", "/children/0/redrive_allow_policy")
    cite(snapshot, queue, "/redrive_allow_policy/redrivePermission")
    snapshot.validate_snapshot_integrity()
    cite(snapshot, queue, "/redrive_allow_policy/extra")
    with pytest.raises(ValueError, match="missing key 'extra'"):
        snapshot.validate_snapshot_integrity()


@pytest.mark.parametrize("scope", ["all", "leaf"])
def test_each_resource_is_serialized_at_most_once_per_validation(scope: str) -> None:
    snapshot = snapshot_with_tree(depth=4, width=3)
    root = snapshot.regions[AwsRegion.EU_WEST_1].resources[0]
    resources = list(walk_resources([root]))
    cited = resources if scope == "all" else [resources[-1]]
    for resource in cited:
        cite(snapshot, resource, "/name", "/identity/physical_id")
    profile = cProfile.Profile()
    profile.runcall(snapshot.validate_snapshot_integrity)
    visits = sum(
        stats[1]
        for key, stats in pstats.Stats(profile).stats.items()
        if key[2] == "omit_absent_fields"
    )
    assert visits == len(cited)
