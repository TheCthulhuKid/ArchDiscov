"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.services import load_analysis, load_snapshot, save_snapshot, upgrade_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import ENTRY_POINTS, run_installed_json
from tests.support.lb_exposure_data import FULL_COVERAGE, build, ec2_items
from tests.support.trust_data import schema_one


def aws_snapshot(schema: str):
    snapshot = build(ec2=ec2_items(public_eni=True), coverage=FULL_COVERAGE)
    if schema == "1":
        return schema_one(snapshot)
    return upgrade_snapshot(snapshot) if schema == "3" else snapshot


def analyze_arguments(snapshot_path, result_path, *extra):
    return [
        "analyze",
        "--snapshot",
        str(snapshot_path),
        "--output",
        str(result_path),
        "--analyzer",
        "internet_exposure",
        *extra,
        "--json",
    ]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("schema", ["1", "2", "3"])
def test_installed_internet_exposure_journey(tmp_path, entry_point, schema):
    snapshot_path = save_snapshot(aws_snapshot(schema), tmp_path / "snapshot.json")
    result_path = tmp_path / "result.json"

    saved = run_installed_json(
        entry_point,
        tmp_path,
        *analyze_arguments(snapshot_path, result_path, "--max-depth", "2", "--result-limit", "50"),
    )
    shown = run_installed_json(
        entry_point,
        tmp_path,
        "findings",
        str(result_path),
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert shown["findings"] == saved["findings"]
    result = load_analysis(result_path, snapshot=load_snapshot(snapshot_path))
    assert (result.parameters.max_depth, result.parameters.result_limit) == (2, 50)

    exposures = [f for f in shown["findings"] if f["internet_exposure"]]
    kinds = {f["internet_exposure"]["entry_kind"] for f in exposures}
    assert kinds == {"public_address", "load_balancer"}
    balancer = next(f for f in exposures if f["internet_exposure"]["entry_kind"] == "load_balancer")
    assert balancer["internet_exposure"]["decision"] == "allow"
    assert balancer["internet_exposure"]["observed_target_health"] == ["unhealthy"]
    assert len(balancer["resources"]) == 2  # the entry point, then its private backend

    inspected = run_installed_json(
        entry_point,
        tmp_path,
        "evidence",
        balancer["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["evidence"]["resource_id"]

    before = result_path.read_bytes()
    conflict = run_installed_json(
        entry_point, tmp_path, *analyze_arguments(snapshot_path, result_path), expect=3
    )
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert result_path.read_bytes() == before


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_azure_journey_returns_an_actionable_unsupported_result(tmp_path, entry_point):
    azure = FIXTURES_ROOT / "snapshots" / "azure.json"
    assert GraphIndex(load_snapshot(azure)).resource_ids
    result_path = tmp_path / "result.json"
    run_installed_json(entry_point, tmp_path, *analyze_arguments(azure, result_path))
    shown = run_installed_json(
        entry_point,
        tmp_path,
        "findings",
        str(result_path),
        "--snapshot",
        str(azure),
        "--json",
    )
    [finding] = shown["findings"]
    assert finding["status"] == "insufficient_evidence"
    assert finding["internet_exposure"] is None
    assert finding["limitations"][0]["code"] == "unsupported_platform"
    assert "internet exposure" in finding["limitations"][0]["message"]
