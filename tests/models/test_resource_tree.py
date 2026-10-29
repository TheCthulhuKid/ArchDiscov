from archdiscov.models.architecture import Ec2Instance, Resource, ResourceIdentity, Subnet, Vpc
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resource_tree import walk_resources


def resource(cls: type[Resource], identifier: str) -> Resource:
    return cls(
        identity=ResourceIdentity(
            account_id="123456789012",
            region=AwsRegion.EU_WEST_1,
            service=AwsService.EC2,
            physical_id=identifier,
        )
    )


def test_pre_order_preserves_root_child_order_and_object_identity() -> None:
    vpc = resource(Vpc, "vpc")
    subnet = resource(Subnet, "subnet")
    instance = resource(Ec2Instance, "instance")
    sibling = resource(Subnet, "sibling")
    other_root = resource(Vpc, "other")
    vpc.children = [subnet, sibling]
    subnet.children = [instance]
    expected = [vpc, subnet, instance, sibling, other_root]
    actual = list(walk_resources(iter([vpc, other_root])))
    assert len(actual) == len(expected)
    assert all(left is right for left, right in zip(actual, expected, strict=True))
