from copy import deepcopy

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.graph.identity import native_resource_type
from archdiscov.models.architecture import LocalTarget, RegionArchitecture
from archdiscov.models.enums import AwsRegion, QueryErrorCode, QueryTruncationReason
from archdiscov.models.query.requests import QuerySearchRequest
from archdiscov.models.query.results import QuerySearchResult
from archdiscov.services import QueryError, load_snapshot, search_resources, upgrade_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def snapshot(version):
    result = load_snapshot(FIXTURES / ("azure.json" if version == "azure" else "hierarchy.json"))
    if version == "3":
        result = upgrade_snapshot(result)
    elif version == "2":
        # Convert the legacy dependency wire shape, preserving its local meaning.
        result.schema_version = "2"
        for edges in result.dependencies_by_region.values():
            for edge in edges:
                edge.target = LocalTarget(resource_id=edge.target_id)
                edge.target_id = None
    return result


@pytest.mark.parametrize("version", ["1", "2", "3", "azure"])
def test_search_filters_order_and_detached_typed_round_trip(version):
    source = snapshot(version)
    index = GraphIndex(source)
    result = search_resources(index, {})
    assert [resource.id for resource in result.resources] == list(index.resource_ids)
    assert QuerySearchResult.model_validate_json(result.model_dump_json()) == result
    resource = result.resources[-1]
    selected = search_resources(
        index, {"resource_id": resource.id, "native_type": native_resource_type(resource)}
    )
    assert selected.resources == [resource]
    assert type(selected.resources[0]) is type(resource)
    assert search_resources(index, {"resource_id": resource.id, "name": "absent"}).resources == []
    result.resources[0].attributes["changed"] = True
    result.scope.__dict__.clear()
    assert "changed" not in index.get_resource(index.resource_ids[0]).attributes
    assert search_resources(index, {}).scope == index.scope


@pytest.mark.parametrize("version", ["1", "3", "azure"])
def test_qualified_physical_matching_and_case_rules(version):
    index = GraphIndex(snapshot(version))
    resource = next(item for item in index.resources() if item.identity.physical_id)
    query = {
        "physical_identifier": {
            "scope": index.scope.model_dump(mode="json"),
            "native_type": native_resource_type(resource),
            "physical_id": resource.identity.physical_id,
        }
    }
    assert [item.id for item in search_resources(index, query).resources] == [resource.id]
    upper = deepcopy(query)
    upper["physical_identifier"]["native_type"] = upper["physical_identifier"][
        "native_type"
    ].upper()
    upper["physical_identifier"]["physical_id"] = upper["physical_identifier"][
        "physical_id"
    ].upper()
    assert bool(search_resources(index, upper).resources) == (version == "azure")
    native_upper = native_resource_type(resource).upper()
    assert bool(search_resources(index, {"native_type": native_upper}).resources) == (
        version == "azure"
    )
    wrong_scope = deepcopy(query)
    scope = wrong_scope["physical_identifier"]["scope"]
    scope["subscription_id" if version == "azure" else "account_id"] = (
        "22222222-2222-2222-2222-222222222222" if version == "azure" else "999999999999"
    )
    assert search_resources(index, wrong_scope).resources == []


def test_limits_only_report_actually_omitted_matches():
    index = GraphIndex(snapshot("1"))
    count = len(index.resource_ids)
    assert search_resources(index, {"limit_result": count}).truncation.reasons == []
    limited = search_resources(index, {"limit_result": count - 1})
    assert [item.id for item in limited.resources] == list(index.resource_ids[:-1])
    assert limited.truncation.reasons == [QueryTruncationReason.RESULT_LIMIT]
    assert (
        search_resources(index, {"resource_id": "absent", "limit_result": 1}).truncation.reasons
        == []
    )


def test_logical_only_and_unknown_native_types():
    index = GraphIndex(load_snapshot(FIXTURES / "terraform-plan.json"))
    resource = index.resources()[0]
    assert resource.identity.physical_id is None
    assert search_resources(index, {"resource_id": resource.id}).resources == [resource]
    unknown = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    assert len(search_resources(unknown, {"native_type": "AWS::EC2::SecurityGroup"}).resources) == 1
    assert search_resources(unknown, {"native_type": "unknown"}).resources == []


def test_physical_ambiguity_keeps_all_regional_candidates():
    source = load_snapshot(FIXTURES / "aws.json")

    original = source.regions[AwsRegion.EU_WEST_1].resources[0]
    duplicate = original.model_copy(deep=True)
    duplicate.children = []
    duplicate.evidence_ids = []
    duplicate.fact_evidence = {}
    duplicate.identity.region = AwsRegion.US_EAST_1
    duplicate.id = duplicate.identity.stable_id
    source.regions[AwsRegion.US_EAST_1] = RegionArchitecture(resources=[duplicate])
    source.coverage.regions.append(AwsRegion.US_EAST_1)
    index = GraphIndex(source)
    result = search_resources(
        index,
        {
            "physical_identifier": {
                "scope": index.scope,
                "native_type": native_resource_type(original),
                "physical_id": original.identity.physical_id,
            }
        },
    )
    assert [item.id for item in result.resources] == sorted([original.id, duplicate.id])


@pytest.mark.parametrize(
    "data", [{"limit_result": 0}, {"native_type": " "}, {"extra": "protected"}]
)
def test_invalid_requests_have_fixed_public_errors(data):
    with pytest.raises(QueryError) as caught:
        search_resources(GraphIndex(snapshot("1")), data)
    assert caught.value.code == QueryErrorCode.INVALID_REQUEST
    assert str(caught.value) == "Invalid query request."


def test_mutated_request_is_revalidated_without_rebuilding_index(monkeypatch):
    index = GraphIndex(snapshot("1"))
    request = QuerySearchRequest()
    request.limit_result = 0
    with pytest.raises(QueryError, match="Invalid query request"):
        search_resources(index, request)
    monkeypatch.setattr(GraphIndex, "__init__", lambda *args: pytest.fail("index rebuilt"))
    assert search_resources(index, {}).resources


def test_unexpected_programming_errors_are_not_disguised(monkeypatch):
    index = GraphIndex(snapshot("1"))

    def broken():
        raise TypeError("implementation defect")

    monkeypatch.setattr(index, "resources", broken)
    with pytest.raises(TypeError, match="implementation defect"):
        search_resources(index, {})
