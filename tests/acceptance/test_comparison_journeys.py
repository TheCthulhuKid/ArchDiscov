"""Credential-free fixture -> compare -> save -> show -> evidence journeys."""

import json

from archdiscov.services import (
    load_comparison,
    load_snapshot,
    save_snapshot,
)
from tests.comparison.snapshots import coverage
from tests.paths import FIXTURES_ROOT
from tests.support.comparison_data import journey_snapshot, rid
from tests.support.installed_cli import run_installed_json

DELETE = {"actions": ["delete"], "value_scope": "before"}


def journey(tmp_path, left, right):
    left_path = save_snapshot(left, tmp_path / "left.json")
    right_path = save_snapshot(right, tmp_path / "right.json")
    result_path = tmp_path / "comparison.json"
    saved = run_installed_json(
        tmp_path,
        *("compare", "--left", str(left_path), "--right", str(right_path)),
        *("--output", str(result_path), "--json"),
    )
    shown = run_installed_json(
        tmp_path,
        *("comparison", str(result_path), "--left", str(left_path), "--right", str(right_path)),
        "--json",
    )
    assert shown["matched"] == saved["matched"] and shown["unmatched"] == saved["unmatched"]
    result = load_comparison(
        result_path, left=load_snapshot(left_path), right=load_snapshot(right_path)
    )
    return shown, result, left_path, right_path


def test_installed_observed_pair_journey_cites_evidence_on_both_sides(tmp_path):
    shown, result, left_path, right_path = journey(
        tmp_path,
        journey_snapshot("aws", 1, seed=1, schema="3"),
        journey_snapshot("aws", 2, seed=2, schema="3"),
    )
    [pair] = result.matched
    [difference] = pair.differences
    assert (difference.path, difference.left_value, difference.right_value) == (
        "/attributes/a",
        1,
        2,
    )
    assert difference.left_evidence_ids == difference.right_evidence_ids == ["cite"]
    assert shown["left"]["source_kind"] == "aws" and shown["right"]["side"] == "right"
    for path in (left_path, right_path):
        cited = run_installed_json(tmp_path, "evidence", "cite", "--snapshot", str(path), "--json")
        assert cited["evidence_id"] == "cite"
        assert cited["resource"]["id"] == rid("vpc-1")


def test_installed_observed_against_terraform_plan_reports_planned_lifecycle(tmp_path):
    plan = journey_snapshot("terraform_plan", 2, seed=2, resource={"lifecycle": DELETE})
    shown, result, *_ = journey(tmp_path, journey_snapshot("aws", 1, seed=1), plan)
    [pair] = result.matched
    assert [str(q.kind) for q in pair.qualifications] == ["planned_lifecycle"]
    assert result.unmatched == []
    assert [item["kind"] for item in shown["limitations"]] == ["source_kind_mismatch"]


def test_installed_observed_against_state_and_cloudformation(tmp_path):
    for source in ("terraform_state", "cloudformation"):
        directory = tmp_path / source
        directory.mkdir()
        right = journey_snapshot(source, 2, seed=2, operations=[coverage("failed")])
        _shown, result, *_ = journey(directory, journey_snapshot("aws", 1, seed=1), right)
        assert str(result.right.source_kind) == source
        [pair] = result.matched
        assert any(str(q.kind) == "limited_coverage" for q in pair.differences[0].qualifications)


def test_installed_cross_cloud_pair_matches_nothing(tmp_path):
    aws = load_snapshot(FIXTURES_ROOT / "snapshots" / "aws.json")
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    shown, result, *_ = journey(tmp_path, aws, azure)
    assert result.matched == [] and shown["matched"] == []
    assert {item["kind"] for item in shown["limitations"]} >= {"cross_cloud"}
    assert {item["side"] for item in shown["unmatched"]} == {"left", "right"}
    assert json.dumps(shown["left"]["scope"]) != json.dumps(shown["right"]["scope"])
