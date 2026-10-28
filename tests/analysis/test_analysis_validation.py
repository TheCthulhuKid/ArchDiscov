import json
from copy import deepcopy

import pytest

from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.analysis_validation import (
    AnalysisValidationError,
    validate_analysis_result,
)
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT
INSTANCE = "123456789012/eu-west-1/ec2/physical/i-web"
ROLE = "123456789012/global/iam/physical/role-app"


def fixture(name):
    return json.loads((FIXTURES / "analysis" / f"{name}.json").read_text())


@pytest.fixture
def path_data():
    return fixture("partial-path")


@pytest.fixture
def snapshot():
    return load_snapshot(FIXTURES / "snapshots" / "dependency-targets.json")


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize(
    "name,snapshot_path",
    [
        ("ownership", "analysis/ownership-snapshot.json"),
        ("partial-path", "snapshots/dependency-targets.json"),
    ],
)
def test_handmade_results_round_trip(name, snapshot_path, modern):
    snapshot = load_snapshot(FIXTURES / snapshot_path)
    if modern:
        snapshot = upgrade_snapshot(snapshot)
    result = validate_analysis_result(fixture(name), snapshot)
    restored = validate_analysis_result(
        AnalysisResult.model_validate_json(result.model_dump_json()), snapshot
    )
    assert restored == result
    assert restored.findings[0].id == result.findings[0].id
    assert result.findings[0].evidence_ids
    if name == "partial-path":
        assert (
            result.findings[0].limitations[0].unresolved_target.target
            == result.findings[0].path[0].target
        )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("resource", "missing", "resource_unknown"),
        ("evidence", "missing", "evidence_unknown"),
        ("scope", "210987654321", "resource_scope_mismatch"),
        ("path_evidence", "role", "evidence_owner_mismatch"),
        ("relationship", "forwards_to", "dependency_unknown"),
        ("unresolved", "invented", "unresolved_target_unknown"),
    ],
)
def test_isolated_reference_defects(path_data, snapshot, field, value, code):
    finding = path_data["findings"][0]
    match field:
        case "resource":
            finding["resources"][0]["resource_id"] = value
        case "scope":
            finding["resources"][0]["scope"]["account_id"] = value
        case "evidence":
            finding["evidence_ids"] = [value]
        case "path_evidence":
            finding["path"][0]["evidence_ids"] = [value]
        case "relationship":
            finding["path"][0]["relationship"] = value
        case "unresolved":
            finding["limitations"][0]["unresolved_target"]["target"]["source_path"] = value
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == code


def test_wrong_snapshot_uuid(path_data, snapshot):
    other = "00000000-0000-4000-8000-000000000099"
    path_data["snapshot_id"] = path_data["findings"][0]["snapshot_id"] = other
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "snapshot_mismatch"


@pytest.mark.parametrize("target_index", [0, 1, 2])
def test_local_external_unresolved_edges(path_data, snapshot, target_index):
    edge = list(SnapshotAccess.from_snapshot(snapshot).dependencies())[target_index]
    finding = path_data["findings"][0]
    finding["path"] = [{"kind": "dependency", **edge.model_dump(mode="json", exclude={"location"})}]
    assert validate_analysis_result(path_data, snapshot).findings[0].path[0].target == edge.target


def test_containment_and_ordered_path(path_data, snapshot):
    finding = path_data["findings"][0]
    vpc = "123456789012/eu-west-1/ec2/physical/vpc-main"
    subnet = "123456789012/eu-west-1/ec2/physical/subnet-app"
    finding["path"].insert(0, {"kind": "containment", "source_id": subnet, "target_id": INSTANCE})
    finding["path"].insert(0, {"kind": "containment", "source_id": vpc, "target_id": subnet})
    assert len(validate_analysis_result(path_data, snapshot).findings[0].path) == 3
    finding["path"][0]["target_id"] = INSTANCE
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "containment_unknown"


@pytest.mark.parametrize("terminal", [False, True])
def test_disconnected_or_nonlocal_continuation(path_data, snapshot, terminal):
    step = deepcopy(path_data["findings"][0]["path"][0])
    if not terminal:
        path_data["findings"][0]["path"][0].update(
            target={"kind": "local", "resource_id": ROLE}, relationship="assumes_role"
        )
    path_data["findings"][0]["path"].append(step)
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "path_disconnected"


def azure_result(snapshot):
    access = SnapshotAccess.from_snapshot(snapshot)
    vm = list(access.resources())[-1]
    edge = next(access.dependencies())
    operation = access.coverage.operations[-1]
    return {
        "snapshot_id": str(snapshot.snapshot_id),
        "analyzer": "ownership",
        "analyzer_version": "1",
        "parameters": {"kind": "ownership"},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": [
            {
                "snapshot_id": str(snapshot.snapshot_id),
                "status": "partial",
                "summary": "Fixture contract only.",
                "evidence_ids": ["vm-attachment"],
                "resources": [{"resource_id": vm.id, "scope": access.scope.model_dump()}],
                "path": [{"kind": "dependency", **edge.model_dump(exclude={"location"})}],
                "limitations": [
                    {
                        "code": "coverage_gap",
                        "message": "Unavailable operation.",
                        "coverage": operation.model_dump(
                            exclude={
                                "status",
                                "started_at",
                                "completed_at",
                                "result_count",
                                "failures",
                                "unresolved_values",
                                "reason",
                            }
                        ),
                    }
                ],
            }
        ],
    }


def test_azure_references_coverage_and_no_mutation():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "azure.json")
    data = azure_result(snapshot)
    original = deepcopy(data)
    before = snapshot.model_dump_json()
    result = validate_analysis_result(data, snapshot)
    assert data == original and snapshot.model_dump_json() == before
    assert result.findings[0].resources[0].scope.platform == "azure"
    result.findings[0].summary = "changed"
    assert data == original


def test_complete_coverage_context_required():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "azure.json")
    data = azure_result(snapshot)
    data["findings"][0]["limitations"][0]["coverage"]["location"] = "westus"
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == "coverage_unknown"


def test_azure_service_case_matches_snapshot_rules():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "azure.json")
    data = azure_result(snapshot)
    data["findings"][0]["limitations"][0]["coverage"]["service"] = "microsoft.compute"
    validate_analysis_result(data, snapshot)


def test_invalid_diagnostics_exclude_input_context_and_dynamic_keys(path_data, snapshot):
    marker = "PRIVATE-INPUT-DO-NOT-DISCLOSE"
    path_data[marker] = marker
    path_data["parameters"] = {"kind": marker}
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert marker not in str(error.value)
    assert marker not in repr(error.value.details)
    assert error.value.__cause__ is not None


def test_mutated_model_is_not_trusted(path_data, snapshot):
    result = validate_analysis_result(path_data, snapshot)
    result.findings[0].resources[0].resource_id = "missing"
    with pytest.raises(AnalysisValidationError):
        validate_analysis_result(result, snapshot)


def test_mapping_with_mutated_nested_models_is_revalidated(path_data, snapshot):
    result = validate_analysis_result(path_data, snapshot)
    nested = result.findings[0]
    nested.limitations.clear()
    path_data["findings"] = [nested]
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].location[:2] == ("findings", 0)


def test_missing_selected_parameter_resource(path_data, snapshot):
    path_data["parameters"]["resource_id"] = "missing"
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "resource_unknown"
    assert error.value.details[0].location == ("parameters", "resource_id")


def test_local_edge_must_resolve_to_stored_target(path_data, snapshot):
    path_data["findings"][0]["path"][0]["target"] = {"kind": "local", "resource_id": "missing"}
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "resource_unknown"


@pytest.mark.parametrize("context", ["limitation", "unresolved"])
def test_limitation_citations_are_validated(path_data, snapshot, context):
    limitation = path_data["findings"][0]["limitations"][0]
    if context == "limitation":
        limitation["evidence_ids"] = ["missing"]
        expected = "evidence_unknown"
    else:
        limitation["unresolved_target"]["evidence_ids"] = ["role"]
        expected = "evidence_owner_mismatch"
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == expected


def test_source_owned_evidence_must_also_cite_the_actual_dependency(path_data, snapshot):
    snapshot = upgrade_snapshot(snapshot)
    snapshot.evidence["uncited"] = snapshot.evidence["instance"].model_copy(deep=True)
    path_data["findings"][0]["path"][0]["evidence_ids"] = ["uncited"]
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == "dependency_unknown"


def test_schema_one_containment_with_source_owned_evidence():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "hierarchy.json")
    data = fixture("partial-path")
    data["snapshot_id"] = data["findings"][0]["snapshot_id"] = str(snapshot.snapshot_id)
    data["findings"][0]["limitations"] = [{"code": "scope", "message": "Contract example."}]
    # The instance has no children, so cite the actual subnet -> instance edge without evidence.
    data["findings"][0]["path"] = [
        {
            "kind": "containment",
            "source_id": "123456789012/eu-west-1/ec2/physical/subnet-app",
            "target_id": INSTANCE,
        }
    ]
    validate_analysis_result(data, snapshot)
    data["findings"][0]["path"][0]["evidence_ids"] = ["instance"]
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == "evidence_owner_mismatch"


def test_aws_coverage_reference_uses_full_context():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "partial-coverage.json")
    access = SnapshotAccess.from_snapshot(snapshot)
    resource = next(access.resources())
    data = fixture("ownership")
    data["snapshot_id"] = data["findings"][0]["snapshot_id"] = str(snapshot.snapshot_id)
    finding = data["findings"][0]
    finding.update(
        status="partial",
        evidence_ids=["sg-rule"],
        resources=[{"resource_id": resource.id, "scope": access.scope.model_dump()}],
        limitations=[
            {
                "code": "coverage_gap",
                "message": "Routes unavailable.",
                "coverage": {
                    "scope": access.scope.model_dump(),
                    "location": "eu-west-1",
                    "service": "ec2",
                    "operation": "DescribeRouteTables",
                },
            }
        ],
    )
    validate_analysis_result(data, snapshot)
    finding["limitations"][0]["coverage"]["service"] = "EC2"
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == "coverage_unknown"
