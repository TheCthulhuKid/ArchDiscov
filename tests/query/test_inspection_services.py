import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.cloud.context import evidence_context_matches
from archdiscov.models.enums import QueryErrorCode
from archdiscov.models.query.results import QueryInspectResult
from archdiscov.services import QueryError, inspect_resource, load_snapshot, search_resources
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize("fixture", ["partial-coverage", "azure"])
def test_inspection_preserves_exact_owner_facts_and_context(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    for resource in index.resources():
        result = inspect_resource(index, {"resource_id": resource.id})
        assert result.resource == resource
        assert result.evidence == {
            key: index.get_evidence(key) for key in index.resource_evidence_ids(resource.id)
        }
        assert result.coverage == [
            record
            for record in index.coverage_records()
            if evidence_context_matches(
                resource.identity, index.scope, record.scope, record.location, record.service
            )
        ]
        assert QueryInspectResult.model_validate_json(result.model_dump_json()) == result


def test_uncited_unavailable_evidence_and_limited_coverage_are_preserved():
    index = GraphIndex(load_snapshot(FIXTURES / "azure.json"))
    resource = next(
        item
        for item in index.resources()
        if item.resource_type == "Microsoft.Compute/virtualMachines"
    )
    result = inspect_resource(index, {"resource_id": resource.id})
    assert result.evidence["vm-unavailable"].value_status == "unresolved"
    assert [item.status for item in result.coverage] == ["failed", "incomplete"]
    assert {item.status for item in result.limitations if item.kind == "coverage_limited"} == {
        "failed",
        "incomplete",
    }


def test_successful_zero_is_not_missing_or_limited():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    result = inspect_resource(index, {"resource_id": index.resource_ids[0]})
    zero = next(item for item in result.coverage if item.operation == "DescribeInstances")
    assert zero.status == "success" and zero.result_count == 0
    assert [
        item.coverage.operation for item in result.limitations if item.kind == "coverage_limited"
    ] == ["DescribeRouteTables"]
    assert not any(item.kind == "coverage_missing" for item in result.limitations)


def test_missing_named_operation_is_distinct_from_missing_generic_context():
    source = load_snapshot(FIXTURES / "partial-coverage.json")
    source.coverage.operations = []
    index = GraphIndex(source)
    result = inspect_resource(index, {"resource_id": index.resource_ids[0]})
    missing = [item for item in result.limitations if item.kind == "coverage_missing"]
    assert len(missing) == 1
    assert missing[0].coverage.operation == "DescribeSecurityGroups"
    assert result.coverage == []
    assert any(
        item.kind == "provenance_unavailable" and item.fact_path == "/identity"
        for item in result.limitations
    )


def test_unknown_id_is_fixed_error_but_invalid_shape_is_different():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    for request, code in [
        ({"resource_id": "protected-secret-id"}, QueryErrorCode.RESOURCE_NOT_FOUND),
        ({"resource_id": " "}, QueryErrorCode.INVALID_REQUEST),
    ]:
        with pytest.raises(QueryError) as caught:
            inspect_resource(index, request)
        assert caught.value.code == code
        assert "protected-secret-id" not in str(caught.value)


def test_search_preserves_limited_coverage_even_when_no_resources_match():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    result = search_resources(index, {"resource_id": "absent"})
    assert result.resources == []
    [limited] = [item for item in result.limitations if item.kind == "coverage_limited"]
    assert limited.status == "failed"
    assert limited.coverage.operation == "DescribeRouteTables"
