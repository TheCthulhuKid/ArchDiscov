import json

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.graph import GraphIndex
from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import ENTRY_POINTS, run_installed_json

FIXTURES = FIXTURES_ROOT / "snapshots"
NETWORK = FIXTURES / "network.json"
RTB = "123456789012/eu-west-1/ec2/physical/rtb-main"
runner = CliRunner()


def invoke(*arguments):
    return runner.invoke(app, ["query", *arguments])


def payload(*arguments):
    result = invoke(*arguments, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_every_query_runs_against_every_fixture(fixture):
    index = GraphIndex(load_snapshot(fixture))
    snapshot = ["--snapshot", str(fixture)]
    found = payload("search", *snapshot)
    assert [item["id"] for item in found["resources"]] == list(index.resource_ids)
    assert found["snapshot_id"] == str(index.snapshot_id)
    start = index.resource_ids[0]
    assert payload("inspect", start, *snapshot)["resource"]["id"] == start
    assert payload("neighbours", start, "--edges", "both", "--direction", "both", *snapshot)
    assert payload("paths", start, "--edges", "both", "--direction", "both", *snapshot)
    for command in (["search"], ["inspect", start], ["neighbours", start], ["paths", start]):
        readable = invoke(*command, *snapshot)
        assert readable.exit_code == 0, readable.output
        assert str(index.snapshot_id) in readable.stdout


def test_query_cites_evidence_that_the_evidence_command_resolves():
    result = payload("neighbours", RTB, "--snapshot", str(NETWORK))
    cited = [i for step in result["steps"] for i in step["edge"]["edge"]["evidence_ids"]]
    assert "rtb-igw" in cited
    assert [item["kind"] for item in result["limitations"]].count("external_target") == 1
    evidence = runner.invoke(app, ["evidence", "rtb-igw", "--snapshot", str(NETWORK), "--json"])
    assert evidence.exit_code == 0, evidence.output
    assert json.loads(evidence.stdout)["resource"]["id"] == RTB


def test_filters_direction_and_containment_options():
    snapshot = ["--snapshot", str(NETWORK)]
    kinds = ["--attachment-kind", "route_target", "--relationship", "attached_to"]
    assert len(payload("neighbours", RTB, *kinds, *snapshot)["steps"]) == 2
    assert payload("neighbours", RTB, "--relationship", "assumes_role", *snapshot)["steps"] == []
    contained = payload(
        "neighbours", RTB, "--direction", "incoming", "--edges", "containment", *snapshot
    )
    [step] = contained["steps"]
    assert step["direction"] == "incoming"
    assert step["edge"]["kind"] == "containment"
    found = payload("search", "--tag", "Owner", "--type", "AWS::EC2::RouteTable", *snapshot)
    assert found["resources"] == []
    assert any(item["kind"] == "provenance_unavailable" for item in found["limitations"])
    by_physical = payload(
        "search", "--type", "AWS::EC2::RouteTable", "--physical-id", "rtb-main", *snapshot
    )
    assert [item["id"] for item in by_physical["resources"]] == [RTB]


def test_truncation_and_limitations_are_visible_in_both_formats():
    snapshot = ["--snapshot", str(NETWORK)]
    assert payload("search", "--limit", "1", *snapshot)["truncation"]["reasons"] == ["result_limit"]
    readable = invoke("search", "--limit", "1", *snapshot)
    assert "Truncated: result_limit" in readable.stdout
    assert "not proof of absence" in readable.stdout
    paths = invoke("paths", RTB, "--depth", "1", *snapshot)
    assert "Limitations:" in paths.stdout
    assert "not reachability or effective access" in paths.stdout
    assert payload("paths", RTB, "--work-limit", "1", *snapshot)["truncation"]["reasons"] == [
        "work_limit"
    ]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(
    ("arguments", "code", "error"),
    [
        (["inspect", "absent"], 4, "resource_not_found"),
        (["neighbours", "absent"], 4, "resource_not_found"),
        (["search", "--limit", "0"], 2, "invalid_request"),
        (["paths", RTB, "--depth", "11"], 2, "invalid_request"),
        (["search", "--physical-id", "rtb-main"], 2, "invalid_request"),
        (
            ["neighbours", RTB, "--edges", "containment", "--relationship", "attached_to"],
            2,
            "invalid_request",
        ),
    ],
)
def test_expected_failures_have_safe_messages_and_exit_codes(arguments, code, error, json_output):
    result = invoke(*arguments, "--snapshot", str(NETWORK), *(["--json"] if json_output else []))
    assert result.exit_code == code
    text = result.stdout if json_output else result.output
    assert error in text
    assert "Traceback" not in text
    assert "absent" not in text


def test_snapshot_failures_use_snapshot_exit_codes(tmp_path):
    missing = invoke("search", "--snapshot", str(tmp_path / "missing.json"))
    assert missing.exit_code == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert invoke("search", "--snapshot", str(broken)).exit_code == 2


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_installed_journey_outside_repository(tmp_path, entry_point):
    def run(*arguments):
        return run_installed_json(entry_point, tmp_path, *arguments)

    snapshot = str(NETWORK)
    neighbours = run("query", "neighbours", RTB, "--snapshot", snapshot, "--json")
    evidence_id = neighbours["steps"][0]["edge"]["edge"]["evidence_ids"][0]
    inspected = run("evidence", evidence_id, "--snapshot", snapshot, "--json")
    assert inspected["resource"]["id"] == RTB
    assert run("query", "paths", RTB, "--snapshot", snapshot, "--json")["paths"]
