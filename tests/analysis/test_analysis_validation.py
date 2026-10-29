import json
from copy import deepcopy

import pytest

from archdiscov.models.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.analysis_validation import (
    AnalysisValidationError,
    validate_analysis_result,
)
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT
PREFIX = "123456789012/eu-west-1/ec2/physical/"
INSTANCE, SUBNET, VPC = PREFIX + "i-web", PREFIX + "subnet-app", PREFIX + "vpc-main"
OTHER_SNAPSHOT = "00000000-0000-4000-8000-000000000099"
FINDING = ("findings", 0)
RESOURCE = (*FINDING, "resources", 0)
STEP = (*FINDING, "path", 0)
UNRESOLVED = (*FINDING, "limitations", 0, "unresolved_target")


def fixture(name):
    return json.loads((FIXTURES / "analysis" / f"{name}.json").read_text())


def containment(source, target):
    return {"kind": "containment", "source_id": source, "target_id": target}


@pytest.fixture
def path_data():
    return fixture("partial-path")


@pytest.fixture
def snapshot():
    return load_snapshot(FIXTURES / "snapshots" / "dependency-targets.json")


def test_recorded_paths_and_references_validate(path_data, snapshot):
    for selected in (snapshot, upgrade_snapshot(snapshot)):
        [finding] = validate_analysis_result(path_data, selected).findings
        assert finding.limitations[0].unresolved_target.target == finding.path[0].target
    path = path_data["findings"][0]["path"]
    path[:0] = [containment(VPC, SUBNET), containment(SUBNET, INSTANCE)]
    assert len(validate_analysis_result(path_data, snapshot).findings[0].path) == 3
    # Each stored edge is a valid step: local, external, and unresolved targets.
    for edge in SnapshotAccess.from_snapshot(snapshot).dependencies():
        step = {"kind": "dependency", **edge.model_dump(mode="json", exclude={"location"})}
        path_data["findings"][0]["path"] = [step]
        [finding] = validate_analysis_result(path_data, snapshot).findings
        assert finding.path[0].target == edge.target


@pytest.mark.parametrize(
    ("edits", "code"),
    [
        (
            {("snapshot_id",): OTHER_SNAPSHOT, (*FINDING, "snapshot_id"): OTHER_SNAPSHOT},
            "snapshot_mismatch",
        ),
        ({(*RESOURCE, "resource_id"): "missing"}, "resource_unknown"),
        ({(*RESOURCE, "scope", "account_id"): "210987654321"}, "resource_scope_mismatch"),
        ({(*FINDING, "evidence_ids"): ["missing"]}, "evidence_unknown"),
        ({(*STEP, "evidence_ids"): ["role"]}, "evidence_owner_mismatch"),
        ({(*STEP, "relationship"): "forwards_to"}, "dependency_unknown"),
        ({(*FINDING, "path"): [containment(VPC, INSTANCE)]}, "containment_unknown"),
        ({(*FINDING, "path"): [containment(SUBNET, INSTANCE)] * 2}, "path_disconnected"),
        ({(*UNRESOLVED, "target", "source_path"): "invented"}, "unresolved_target_unknown"),
    ],
    ids=[
        "snapshot",
        "resource",
        "scope",
        "evidence",
        "path-evidence",
        "relationship",
        "containment",
        "disconnected",
        "unresolved",
    ],
)
def test_each_reference_defect_is_rejected(path_data, snapshot, edits, code):
    for location, value in edits.items():
        *parents, last = location
        target = path_data
        for key in parents:
            target = target[key]
        target[last] = value
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(path_data, snapshot)
    assert error.value.details[0].type == code


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


def test_azure_coverage_references_need_the_full_recorded_context():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "azure.json")
    data = azure_result(snapshot)
    original = deepcopy(data)
    before = snapshot.model_dump_json()
    result = validate_analysis_result(data, snapshot)
    assert result.findings[0].resources[0].scope.platform == "azure"
    assert data == original and snapshot.model_dump_json() == before
    data["findings"][0]["limitations"][0]["coverage"]["location"] = "westus"
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == "coverage_unknown"
