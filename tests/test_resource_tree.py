import pytest

from archdiscov.ingestion.configuration_relationships import walk_resources as configuration_walk
from archdiscov.ingestion.elbv2.relationships import walk_resources as elbv2_walk
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


@pytest.mark.parametrize("iterator", [list, iter])
def test_pre_order_preserves_root_child_order_and_object_identity(iterator) -> None:
    vpc = resource(Vpc, "vpc")
    subnet = resource(Subnet, "subnet")
    instance = resource(Ec2Instance, "instance")
    sibling = resource(Subnet, "sibling")
    other_root = resource(Vpc, "other")
    vpc.children = [subnet, sibling]
    subnet.children = [instance]
    expected = [vpc, subnet, instance, sibling, other_root]
    actual = list(walk_resources(iterator([vpc, other_root])))
    assert len(actual) == len(expected)
    assert all(left is right for left, right in zip(actual, expected, strict=True))


def test_empty_roots_and_leaf() -> None:
    assert list(walk_resources([])) == []
    assert list(walk_resources(iter([]))) == []
    leaf = resource(Ec2Instance, "leaf")
    assert list(walk_resources([leaf])) == [leaf]


def test_traversal_is_lazy_and_does_not_deduplicate_occurrences() -> None:
    leaf = resource(Ec2Instance, "leaf")
    consumed = []

    def roots():
        consumed.append(1)
        yield leaf
        consumed.append(2)
        yield leaf

    traversal = iter(walk_resources(roots()))
    assert consumed == []
    assert next(traversal) is leaf
    assert consumed == [1]
    assert list(traversal) == [leaf]
    assert consumed == [1, 2]


def test_existing_public_imports_reexport_shared_walker() -> None:
    assert configuration_walk is walk_resources
    assert elbv2_walk is walk_resources
