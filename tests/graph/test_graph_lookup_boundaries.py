import pytest

from archdiscov.graph import GraphIndex, PhysicalResourceKey
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import CloudPlatform
from archdiscov.models.resource_base import Resource
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_direct_azure_key_construction_matches_factory_and_lookup():
    index = GraphIndex(load_snapshot(FIXTURES / "azure.json"))
    resource = index.resources()[0]
    expected = index.physical_key(resource.id)
    direct = PhysicalResourceKey(
        platform=CloudPlatform.AZURE,
        scope=tuple(reversed(expected.scope)),
        native_type=expected.native_type.upper(),
        physical_id=expected.physical_id.upper(),
    )
    assert direct == expected
    assert hash(direct) == hash(expected)
    assert index.find_physical(direct) == (resource,)


def test_direct_key_construction_rejects_blank_matching_values():
    scope = AzureScope(subscription_id="11111111-1111-1111-1111-111111111111")
    with pytest.raises(ValueError, match="native type and physical identifier"):
        PhysicalResourceKey(platform=scope.platform, scope=(), native_type="   ", physical_id="id")


@pytest.mark.parametrize("name", ["hierarchy", "azure"])
def test_resource_listing_copies_each_node_once_and_shares_returned_children(name, monkeypatch):
    index = GraphIndex(load_snapshot(FIXTURES / f"{name}.json"))
    original = Resource.__deepcopy__
    visits = []

    def counted(resource, memo=None):
        visits.append(resource.id)
        return original(resource, memo)

    monkeypatch.setattr(Resource, "__deepcopy__", counted)
    returned = index.resources()
    assert sorted(visits) == list(index.resource_ids)
    by_id = {resource.id: resource for resource in returned}
    parent = next(resource for resource in returned if resource.children)
    child = parent.children[0]
    assert child is by_id[child.id]
    child.attributes["caller_change"] = True
    assert by_id[child.id].attributes["caller_change"] is True
    assert "caller_change" not in index.get_resource(child.id).attributes
    fresh_child = next(r for r in index.resources() if r.id == child.id)
    assert "caller_change" not in fresh_child.attributes
