"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.services import (
    load_analysis,
    load_snapshot,
    save_snapshot,
    upgrade_snapshot,
    validate_snapshot,
)
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import ENTRY_POINTS, run_installed_json
from tests.support.security_group_data import EVIDENCE, IDS, schema_one_data, variant


def aws_snapshot(schema: str):
    if schema == "1":
        return validate_snapshot(schema_one_data())
    snapshot = validate_snapshot(variant())
    return upgrade_snapshot(snapshot) if schema == "3" else snapshot


def journey(entry_point, tmp_path, snapshot_path, resource_id, *extra):
    result_path = tmp_path / "result.json"
    arguments = ["--snapshot", str(snapshot_path)]
    saved = run_installed_json(
        entry_point,
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
    shown = run_installed_json(
        entry_point, tmp_path, "findings", str(result_path), *arguments, "--json"
    )
    assert shown["findings"] == saved["findings"]
    return result_path, shown


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("schema", ["1", "2", "3"])
def test_installed_aws_security_group_journey(tmp_path, entry_point, schema):
    snapshot = aws_snapshot(schema)
    snapshot_path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    result_path, shown = journey(
        entry_point, tmp_path, snapshot_path, IDS["sg-app"], "--max-depth", "3"
    )
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
        entry_point,
        tmp_path,
        "evidence",
        direct["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["resource"]["id"] == IDS["lambda"]

    before = result_path.read_bytes()
    conflict = run_installed_json(
        entry_point,
        tmp_path,
        "analyze",
        "--snapshot",
        str(snapshot_path),
        "--output",
        str(result_path),
        "--analyzer",
        "security_group_impact",
        "--resource-id",
        IDS["sg-app"],
        "--json",
        expect=3,
    )
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert result_path.read_bytes() == before


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_azure_journey_returns_an_actionable_unsupported_result(tmp_path, entry_point):
    azure = FIXTURES_ROOT / "snapshots" / "azure.json"
    selected = GraphIndex(load_snapshot(azure)).resource_ids[0]
    _, shown = journey(entry_point, tmp_path, azure, selected)
    [finding] = shown["findings"]
    assert finding["status"] == "insufficient_evidence"
    assert finding["impact"] is None
    assert finding["limitations"][0]["code"] == "unsupported_platform"
    assert "AWS" in finding["limitations"][0]["message"]
