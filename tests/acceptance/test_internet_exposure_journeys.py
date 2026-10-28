"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

from archdiscov.services import load_analysis, load_snapshot, save_snapshot, upgrade_snapshot
from tests.support.installed_cli import run_installed_json
from tests.support.lb_exposure_data import FULL_COVERAGE, build, ec2_items


def aws_snapshot():
    snapshot = build(ec2=ec2_items(public_eni=True), coverage=FULL_COVERAGE)
    return upgrade_snapshot(snapshot)


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


def test_installed_internet_exposure_journey(tmp_path):
    snapshot_path = save_snapshot(aws_snapshot(), tmp_path / "snapshot.json")
    result_path = tmp_path / "result.json"

    saved = run_installed_json(
        tmp_path,
        *analyze_arguments(snapshot_path, result_path, "--max-depth", "2", "--result-limit", "50"),
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
    assert (result.parameters.max_depth, result.parameters.result_limit) == (2, 50)

    exposures = [f for f in shown["findings"] if f["internet_exposure"]]
    kinds = {f["internet_exposure"]["entry_kind"] for f in exposures}
    assert kinds == {"public_address", "load_balancer"}
    balancer = next(f for f in exposures if f["internet_exposure"]["entry_kind"] == "load_balancer")
    assert balancer["internet_exposure"]["decision"] == "allow"
    assert balancer["internet_exposure"]["observed_target_health"] == ["unhealthy"]
    assert len(balancer["resources"]) == 2  # the entry point, then its private backend

    inspected = run_installed_json(
        tmp_path,
        "evidence",
        balancer["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["evidence"]["resource_id"]
