from copy import deepcopy

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.graph.identity import native_resource_type
from archdiscov.models.architecture import RegionArchitecture
from archdiscov.models.enums import AwsRegion, QueryErrorCode, QueryTruncationReason
from archdiscov.models.query.results import QuerySearchResult
from archdiscov.services import QueryError, load_snapshot, search_resources
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def snapshot(version):
    return load_snapshot(FIXTURES / ("azure.json" if version == "azure" else "hierarchy.json"))


@pytest.mark.parametrize("version", ["1", "azure"])
def test_search_orders_by_stable_id_and_combines_filters(version):
    index = GraphIndex(snapshot(version))
    result = search_resources(index, {})
    assert [resource.id for resource in result.resources] == list(index.resource_ids)
    assert QuerySearchResult.model_validate_json(result.model_dump_json()) == result
    resource = result.resources[-1]
    selected = search_resources(
        index, {"resource_id": resource.id, "native_type": native_resource_type(resource)}
    )
    assert selected.resources == [resource]
    assert search_resources(index, {"resource_id": resource.id, "name": "absent"}).resources == []


@pytest.mark.parametrize("version", ["1", "azure"])
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


def test_invalid_requests_have_fixed_public_errors():
    with pytest.raises(QueryError) as caught:
        search_resources(GraphIndex(snapshot("1")), {"extra": "protected"})
    assert caught.value.code == QueryErrorCode.INVALID_REQUEST
    assert str(caught.value) == "Invalid query request."


def test_unexpected_programming_errors_are_not_disguised(monkeypatch):
    index = GraphIndex(snapshot("1"))

    def broken():
        raise TypeError("implementation defect")

    monkeypatch.setattr(index, "resources", broken)
    with pytest.raises(TypeError, match="implementation defect"):
        search_resources(index, {})
