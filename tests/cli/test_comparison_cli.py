import json

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.services import load_comparison, load_snapshot
from tests.paths import FIXTURES_ROOT

runner = CliRunner()
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
AWS, AZURE, PLAN = (
    SNAPSHOTS / "aws.json",
    SNAPSHOTS / "azure.json",
    SNAPSHOTS / "terraform-plan.json",
)
CLOUDFORMATION = SNAPSHOTS / "cloudformation.json"
PAIRS = [
    (AWS, AWS),
    (AZURE, AZURE),
    (AWS, PLAN),
    (AWS, CLOUDFORMATION),
    (AWS, AZURE),
]


def run(*arguments):
    return runner.invoke(app, [str(item) for item in arguments])


def compare(left, right, output, *extra):
    return run("compare", "--left", left, "--right", right, "--output", output, *extra)


@pytest.mark.parametrize(("left", "right"), PAIRS, ids=lambda path: path.stem)
@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_compare_saves_a_result_that_comparison_verifies(tmp_path, left, right, as_json):
    output = tmp_path / "comparison.json"
    flags = ["--json"] if as_json else []
    saved = compare(left, right, output, *flags)
    assert saved.exit_code == 0, saved.output
    result = load_comparison(output, left=load_snapshot(left), right=load_snapshot(right))
    shown = run("comparison", output, "--left", left, "--right", right, *flags)
    assert shown.exit_code == 0, shown.output
    if as_json:
        payload = json.loads(shown.stdout)
        assert json.loads(saved.stdout)["output_path"] == str(output)
        assert payload["result_path"] == str(output)
        assert payload["left"]["side"] == "left" and payload["right"]["side"] == "right"
        assert payload["left"]["snapshot_id"] == str(result.left.snapshot_id)
        assert len(payload["matched"]) == len(result.matched)
    else:
        assert f"Matched: {len(result.matched)}" in shown.stdout
        assert f"Left: {result.left.snapshot_id}" in shown.stdout
        assert "not verified drift" in shown.stdout


def test_cross_cloud_pairs_report_the_limitation_and_unmatched_resources(tmp_path):
    output = tmp_path / "comparison.json"
    assert compare(AWS, AZURE, output).exit_code == 0
    result = load_comparison(output, left=load_snapshot(AWS), right=load_snapshot(AZURE))
    assert result.matched == [] and result.ambiguous == []
    assert {str(item.kind) for item in result.limitations} == {
        "cross_cloud",
        "source_kind_mismatch",
    }
    shown = run("comparison", output, "--left", AWS, "--right", AZURE)
    assert "Limitation cross_cloud" in shown.stdout
    assert "left only:" in shown.stdout and "right only:" in shown.stdout


def test_an_existing_destination_is_never_overwritten(tmp_path):
    output = tmp_path / "comparison.json"
    output.write_text("keep")
    refused = compare(AWS, AWS, output)
    assert refused.exit_code == 3
    assert output.read_text() == "keep"


def test_result_limit_truncates_and_is_recorded(tmp_path):
    output = tmp_path / "comparison.json"
    assert compare(AWS, AZURE, output, "--result-limit", "1").exit_code == 0
    result = load_comparison(output, left=load_snapshot(AWS), right=load_snapshot(AZURE))
    assert result.result_limit == 1
    assert "result_limit_reached" in {str(item.kind) for item in result.limitations}
    assert compare(AWS, AZURE, tmp_path / "bad.json", "--result-limit", "0").exit_code == 2


def test_an_unreadable_snapshot_names_its_own_side(tmp_path):
    missing = tmp_path / "missing.json"
    failed = compare(AWS, missing, tmp_path / "out.json", "--json")
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["error"]["path"] == str(missing)
    assert not (tmp_path / "out.json").exists()
    failed = compare(missing, AWS, tmp_path / "out.json", "--json")
    assert json.loads(failed.stdout)["error"]["path"] == str(missing)


def test_swapped_snapshots_or_a_tampered_result_fail_verification(tmp_path):
    output = tmp_path / "comparison.json"
    assert compare(AWS, PLAN, output).exit_code == 0
    swapped = run("comparison", output, "--left", PLAN, "--right", AWS, "--json")
    assert swapped.exit_code == 2
    error = json.loads(swapped.stdout)["error"]
    assert error["code"] == "invalid_comparison_result"

    data = json.loads(output.read_text())
    data["limitations"] = []
    output.write_text(json.dumps(data))
    shown = run("comparison", output, "--left", AWS, "--right", PLAN)
    assert shown.exit_code == 2
    assert "invalid_comparison_result" in shown.output
