"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

import pytest

from archdiscov.services import load_analysis, load_snapshot, save_snapshot
from tests.support.installed_cli import run_installed_json
from tests.support.tagged_snapshots import TAG_EVIDENCE, tagged

SOURCES = [
    pytest.param("snapshots/hierarchy.json", True, id="aws-schema-3"),
    pytest.param("snapshots/azure.json", False, id="azure-schema-3"),
]


@pytest.mark.parametrize(("source", "upgraded"), SOURCES)
def test_installed_ownership_journey(tmp_path, source, upgraded):
    snapshot, target = tagged(source, upgraded, tags={"Name": "app"})
    snapshot_path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    result_path = tmp_path / "result.json"

    saved = run_installed_json(
        tmp_path,
        "analyze",
        "--snapshot",
        str(snapshot_path),
        "--output",
        str(result_path),
        "--json",
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
    assert load_analysis(result_path, snapshot=load_snapshot(snapshot_path)).snapshot_id == (
        snapshot.snapshot_id
    )

    finding = next(
        item for item in shown["findings"] if item["resources"][0]["resource_id"] == target
    )
    assert finding["status"] in ("supported", "partial")
    assert finding["evidence_ids"] == [TAG_EVIDENCE]
    inspected = run_installed_json(
        tmp_path,
        "evidence",
        finding["evidence_ids"][0],
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["resource"]["id"] == target
    assert inspected["evidence"]["value"] == {"Name": "app"}
