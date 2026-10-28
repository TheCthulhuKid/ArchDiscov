"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

from archdiscov.services import (
    load_analysis,
    load_snapshot,
    save_snapshot,
    upgrade_snapshot,
    validate_snapshot,
)
from tests.support.installed_cli import run_installed_json
from tests.support.security_group_data import EVIDENCE, IDS, variant


def aws_snapshot():
    return upgrade_snapshot(validate_snapshot(variant()))


def journey(tmp_path, snapshot_path, resource_id, *extra):
    result_path = tmp_path / "result.json"
    arguments = ["--snapshot", str(snapshot_path)]
    saved = run_installed_json(
        tmp_path,
        "analyze",
        *arguments,
        "--output",
        str(result_path),
        "--analyzer",
        "security_group_impact",
        "--resource-id",
        resource_id,
        *extra,
        "--json",
    )
    shown = run_installed_json(tmp_path, "findings", str(result_path), *arguments, "--json")
    assert shown["findings"] == saved["findings"]
    return result_path, shown


def test_installed_aws_security_group_journey(tmp_path):
    snapshot = aws_snapshot()
    snapshot_path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    result_path, shown = journey(tmp_path, snapshot_path, IDS["sg-app"], "--max-depth", "3")
    result = load_analysis(result_path, snapshot=load_snapshot(snapshot_path))
    assert result.parameters.max_depth == 3

    labels = {(f["impact"]["label"], f["impact"]["kind"]) for f in shown["findings"] if f["impact"]}
    assert ("observed", "direct_attachment") in labels
    assert ("potential", "downstream_dependency") in labels
    assert ("observed", "rule_reference") in labels

    direct = next(
        f
        for f in shown["findings"]
        if f["impact"]
        and f["impact"]["kind"] == "direct_attachment"
        and f["resources"][0]["resource_id"] == IDS["lambda"]
    )
    assert direct["evidence_ids"] == [EVIDENCE["lambda"]]
    inspected = run_installed_json(
        tmp_path,
        "evidence",
        direct["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["resource"]["id"] == IDS["lambda"]
