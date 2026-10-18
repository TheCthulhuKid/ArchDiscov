import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.cloud.context import evidence_context_matches
from archdiscov.models.enums import CoverageStatus, QueryErrorCode
from archdiscov.models.query.results import QueryInspectResult
from archdiscov.services import QueryError, inspect_resource, load_snapshot, upgrade_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize(
    "fixture", ["hierarchy", "network", "iam", "load-balancing", "partial-coverage", "azure"]
)
@pytest.mark.parametrize("upgrade", [False, True])
def test_inspection_preserves_exact_owner_facts_and_context(fixture, upgrade):
    snapshot = load_snapshot(FIXTURES / f"{fixture}.json")
    if upgrade and snapshot.schema_version != "3":
        snapshot = upgrade_snapshot(snapshot)
    index = GraphIndex(snapshot)
    for resource in index.resources():
        result = inspect_resource(index, {"resource_id": resource.id})
        assert type(result.resource) is type(resource)
        assert result.resource == resource
        assert list(result.evidence) == list(index.resource_evidence_ids(resource.id))
        for pointer, ids in resource.fact_evidence.items():
            assert all(result.evidence[key] == index.get_evidence(key) for key in ids)
            assert tuple(ids) == index.fact_evidence_ids(resource.id, pointer)
        assert result.coverage == [
            record
            for record in index.coverage_records()
            if evidence_context_matches(
                resource.identity, index.scope, record.scope, record.location, record.service
            )
        ]
        assert QueryInspectResult.model_validate_json(result.model_dump_json()) == result


def test_known_null_unavailable_and_uncited_owner_evidence_are_preserved():
    index = GraphIndex(load_snapshot(FIXTURES / "azure.json"))
    resource = next(
        item
        for item in index.resources()
        if item.resource_type == "Microsoft.Compute/virtualMachines"
    )
    result = inspect_resource(index, {"resource_id": resource.id})
    assert result.resource.vm_size is None
    assert result.evidence["vm-size"].value is None
    assert result.evidence["vm-size"].value_status == "present"
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


def test_inspection_mutations_do_not_change_input_index_or_next_result(monkeypatch):
    source = load_snapshot(FIXTURES / "azure.json")
    before = source.model_dump_json()
    index = GraphIndex(source)
    resource_id = next(
        item.id
        for item in index.resources()
        if item.resource_type == "Microsoft.Compute/virtualMachines"
    )
    expected = inspect_resource(index, {"resource_id": resource_id})
    changed = inspect_resource(index, {"resource_id": resource_id})
    changed.resource.attributes["changed"] = True
    changed.evidence["vm-size"].value = "changed"
    changed.coverage[0].failures.clear()
    changed.scope.subscription_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(GraphIndex, "__init__", lambda *args: pytest.fail("index rebuilt"))
    assert inspect_resource(index, {"resource_id": resource_id}) == expected
    assert source.model_dump_json() == before


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


def test_azure_coverage_matches_case_insensitive_service_and_exact_location():
    source = load_snapshot(FIXTURES / "azure.json")
    source.coverage.operations[0].service = "microsoft.network"
    index = GraphIndex(source)
    resource_id = next(
        item.id
        for item in index.resources()
        if item.resource_type == "Microsoft.Network/virtualNetworks"
    )
    result = inspect_resource(index, {"resource_id": resource_id})
    assert [item.operation for item in result.coverage] == ["fixture.empty-network-list"]
    assert not any(item.kind == "coverage_limited" for item in result.limitations)


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped", "unsupported"])
def test_search_preserves_limited_coverage_even_when_no_resources_match(status):
    from archdiscov.services import search_resources

    source = load_snapshot(FIXTURES / "partial-coverage.json")
    operation = source.coverage.operations[1]
    operation.status = CoverageStatus(status)
    if status == "incomplete":
        operation.result_count = 0
    if status in ("skipped", "unsupported"):
        operation.reason = "Operation was unavailable."
        operation.failures = []
    index = GraphIndex(source)
    result = search_resources(index, {"resource_id": "absent"})
    assert result.resources == []
    limited = [item for item in result.limitations if item.kind == "coverage_limited"]
    assert len(limited) == 1
    assert limited[0].status == status
    assert limited[0].coverage.operation == "DescribeRouteTables"


def test_scope_wide_iam_coverage_is_not_confused_with_regional_coverage():
    source = upgrade_snapshot(load_snapshot(FIXTURES / "iam.json"))
    operation = upgrade_snapshot(
        load_snapshot(FIXTURES / "partial-coverage.json")
    ).coverage.operations[0]
    operation.location = None
    operation.service = "iam"
    operation.operation = "ListRoles"
    source.coverage.operations.append(operation)
    index = GraphIndex(source)
    result = inspect_resource(index, {"resource_id": index.resource_ids[0]})
    assert result.coverage
    assert all(record.location is None and record.service == "iam" for record in result.coverage)
