"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AwsService
from archdiscov.services import load_analysis, load_snapshot, save_snapshot, upgrade_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import run_installed_json
from tests.support.rds_data import INSTANCE
from tests.support.source_data import FULL_COVERAGE, build, rid

DATABASE = rid(INSTANCE, AwsService.RDS)


def aws_snapshot():
    snapshot = build(task={}, function={}, coverage=FULL_COVERAGE)
    return upgrade_snapshot(snapshot)


def analyze_arguments(snapshot_path, result_path, resource_id, *extra):
    return [
        "analyze",
        "--snapshot",
        str(snapshot_path),
        "--output",
        str(result_path),
        "--analyzer",
        "database_access",
        "--resource-id",
        resource_id,
        "--port",
        "5432",
        *extra,
        "--json",
    ]


def test_installed_database_access_journey(tmp_path):
    snapshot_path = save_snapshot(aws_snapshot(), tmp_path / "snapshot.json")
    result_path = tmp_path / "result.json"

    saved = run_installed_json(
        tmp_path,
        *analyze_arguments(snapshot_path, result_path, DATABASE, "--result-limit", "50"),
    )
    shown = run_installed_json(
        tmp_path,
        "findings",
        str(result_path),
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert shown["findings"] == saved["findings"]
    result = load_analysis(result_path, snapshot=load_snapshot(snapshot_path))
    assert (result.parameters.port, result.parameters.result_limit) == (5432, 50)

    with_access = [f for f in shown["findings"] if f["database_access"]]
    access = [f["database_access"] for f in with_access]
    assert {"allow", "unknown"} <= {item["decision"] for item in access}
    assert {item["source_kind"] for item in access} == {
        "ec2_instance",
        "ecs_task",
        "lambda_function",
    }
    assert all(f["resources"][1]["resource_id"] == DATABASE for f in with_access)

    cited = next(f for f in with_access if f["database_access"]["decision"] == "allow")
    inspected = run_installed_json(
        tmp_path,
        "evidence",
        cited["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["evidence"]["resource_id"]

    before = result_path.read_bytes()
    conflict = run_installed_json(
        tmp_path,
        *analyze_arguments(snapshot_path, result_path, DATABASE),
        expect=3,
    )
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert result_path.read_bytes() == before


def test_installed_azure_journey_returns_an_actionable_unsupported_result(tmp_path):
    azure = FIXTURES_ROOT / "snapshots" / "azure.json"
    resource = GraphIndex(load_snapshot(azure)).resource_ids[0]
    result_path = tmp_path / "result.json"
    run_installed_json(tmp_path, *analyze_arguments(azure, result_path, resource))
    shown = run_installed_json(
        tmp_path,
        "findings",
        str(result_path),
        "--snapshot",
        str(azure),
        "--json",
    )
    [finding] = shown["findings"]
    assert finding["status"] == "insufficient_evidence"
    assert finding["database_access"] is None
    assert finding["limitations"][0]["code"] == "unsupported_platform"
    assert "database access" in finding["limitations"][0]["message"]
