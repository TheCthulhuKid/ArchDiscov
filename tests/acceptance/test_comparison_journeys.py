"""Credential-free fixture -> compare -> save -> show -> evidence journeys."""

import json

import pytest

from archdiscov.services import (
    load_comparison,
    load_snapshot,
    save_snapshot,
    upgrade_snapshot,
    validate_snapshot,
)
from tests.comparison.snapshots import aws_resource, aws_snapshot, coverage
from tests.comparison.test_field_comparison import evidence, rid
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import ENTRY_POINTS, run_installed_json

DELETE = {"actions": ["delete"], "value_scope": "before"}


def cited_vpc(physical, value, *, seed, **extra):
    """A VPC whose `a` attribute is cited, so a difference has evidence on its side."""
    resource = {
        **aws_resource(physical=physical, **extra.pop("identity", {})),
        "attributes": {"a": value},
        "evidence_ids": ["cite"],
        "fact_evidence": {"/attributes/a": ["cite"]},
        **extra,
    }
    return resource, {"cite": evidence(seed, physical, "a")}


def build(source, value, *, seed, schema="2", **options):
    resource, cited = cited_vpc("vpc-1", value, seed=seed, **options.pop("resource", {}))
    snapshot = aws_snapshot([resource], source, seed=seed, evidence=cited, **options)
    if schema == "3":
        return upgrade_snapshot(snapshot)
    if schema == "1":
        return validate_snapshot({**snapshot.model_dump(mode="json"), "schema_version": "1"})
    return snapshot


def journey(entry_point, tmp_path, left, right):
    left_path = save_snapshot(left, tmp_path / "left.json")
    right_path = save_snapshot(right, tmp_path / "right.json")
    result_path = tmp_path / "comparison.json"
    saved = run_installed_json(
        entry_point,
        tmp_path,
        *("compare", "--left", str(left_path), "--right", str(right_path)),
        *("--output", str(result_path), "--json"),
    )
    shown = run_installed_json(
        entry_point,
        tmp_path,
        *("comparison", str(result_path), "--left", str(left_path), "--right", str(right_path)),
        "--json",
    )
    assert shown["matched"] == saved["matched"] and shown["unmatched"] == saved["unmatched"]
    result = load_comparison(
        result_path, left=load_snapshot(left_path), right=load_snapshot(right_path)
    )
    return shown, result, left_path, right_path


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("schema", ["1", "2", "3"])
def test_installed_observed_pair_journey_cites_evidence_on_both_sides(
    tmp_path, entry_point, schema
):
    shown, result, left_path, right_path = journey(
        entry_point,
        tmp_path,
        build("aws", 1, seed=1, schema=schema),
        build("aws", 2, seed=2, schema=schema),
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
        cited = run_installed_json(
            ENTRY_POINTS[0], tmp_path, "evidence", "cite", "--snapshot", str(path), "--json"
        )
        assert cited["evidence_id"] == "cite"
        assert cited["resource"]["id"] == rid("vpc-1")


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_observed_against_terraform_plan_reports_planned_lifecycle(tmp_path, entry_point):
    plan = build("terraform_plan", 2, seed=2, resource={"lifecycle": DELETE})
    shown, result, *_ = journey(entry_point, tmp_path, build("aws", 1, seed=1), plan)
    [pair] = result.matched
    assert [str(q.kind) for q in pair.qualifications] == ["planned_lifecycle"]
    assert result.unmatched == []
    assert [item["kind"] for item in shown["limitations"]] == ["source_kind_mismatch"]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_observed_against_state_and_cloudformation(tmp_path, entry_point):
    for source in ("terraform_state", "cloudformation"):
        directory = tmp_path / source
        directory.mkdir()
        right = build(source, 2, seed=2, operations=[coverage("failed")])
        shown, result, *_ = journey(entry_point, directory, build("aws", 1, seed=1), right)
        assert str(result.right.source_kind) == source
        [pair] = result.matched
        assert any(str(q.kind) == "limited_coverage" for q in pair.differences[0].qualifications)


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_cross_cloud_pair_matches_nothing(tmp_path, entry_point):
    aws = load_snapshot(FIXTURES_ROOT / "snapshots" / "aws.json")
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    shown, result, *_ = journey(entry_point, tmp_path, aws, azure)
    assert result.matched == [] and shown["matched"] == []
    assert {item["kind"] for item in shown["limitations"]} >= {"cross_cloud"}
    assert {item["side"] for item in shown["unmatched"]} == {"left", "right"}
    assert json.dumps(shown["left"]["scope"]) != json.dumps(shown["right"]["scope"])
