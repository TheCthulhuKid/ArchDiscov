import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
runner = CliRunner()


@pytest.mark.parametrize(
    "fixture",
    [FIXTURES / name for name in ("partial-coverage.json", "azure.json")],
    ids=lambda path: path.stem,
)
def test_ingest_preserves_the_fixture(tmp_path: Path, fixture: Path) -> None:
    output = tmp_path / "snapshot.json"
    result = runner.invoke(app, ["ingest", str(fixture), "--output", str(output), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    snapshot = load_snapshot(fixture)
    assert payload["snapshot_id"] == str(snapshot.snapshot_id)
    assert payload["path"] == str(output)
    assert payload["source_kind"] == snapshot.source.kind
    assert payload["complete"] == all(op.status == "success" for op in snapshot.coverage.operations)
    assert payload["operations"] == [
        {
            **(
                {
                    "platform": op.scope.platform.value,
                    "scope": op.scope.model_dump(mode="json"),
                    "location": op.location,
                    "service": op.service,
                }
                if isinstance(snapshot, CloudSnapshot)
                else {"region": op.region.value, "service": op.service.value}
            ),
            "operation": op.operation,
            "status": op.status.value,
            "result_count": op.result_count,
            "failures": [failure.code for failure in op.failures],
        }
        for op in snapshot.coverage.operations
    ]
    assert load_snapshot(output) == snapshot
    assert result.stderr == ""


@pytest.mark.parametrize("json_output", [False, True])
def test_offline_ingest_and_evidence_workflow(tmp_path: Path, json_output: bool) -> None:
    output = tmp_path / "snapshot.json"
    flags = ["--json"] if json_output else []
    imported = runner.invoke(
        app, ["ingest", str(FIXTURES / "partial-coverage.json"), "-o", str(output), *flags]
    )
    assert imported.exit_code == 0, imported.output
    original = output.read_bytes()
    inspected = runner.invoke(app, ["evidence", "sg-rule", "--snapshot", str(output), *flags])
    assert inspected.exit_code == 0, inspected.output
    if json_output:
        payload = json.loads(inspected.stdout)
        assert payload["snapshot_path"] == str(output)
        assert payload["evidence_id"] == "sg-rule"
        assert payload["resource"]["id"] == payload["evidence"]["resource_id"]
        assert payload["fact_paths"] == ["/attributes/ingress/0"]
        assert payload["coverage"][1]["failures"][0]["code"] == "UnauthorizedOperation"
    else:
        for text in (
            "Saved snapshot",
            "PARTIAL",
            "eu-west-1 ec2 DescribeRouteTables: failed [UnauthorizedOperation]",
            "DescribeInstances: success (0 results)",
        ):
            assert text in imported.stdout
        for text in (
            str(output),
            "sg-rule",
            "sg-example",
            "DescribeSecurityGroups",
            "SecurityGroups[0].IpPermissions[0]",
            '"FromPort": 443',
            "UnauthorizedOperation",
            "Route-table collection denied",
            "DescribeInstances: success; results: 0",
            "not a completeness guarantee",
        ):
            assert text in inspected.stdout
    assert output.read_bytes() == original


def test_azure_ingest_and_evidence_name_their_cloud_context(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.json"
    imported = runner.invoke(app, ["ingest", str(FIXTURES / "azure.json"), "-o", str(output)])
    assert imported.exit_code == 0, imported.output
    assert "azure eastus Microsoft.Compute fixture.instance-view: failed" in imported.stdout
    inspected = runner.invoke(app, ["evidence", "vm-size", "--snapshot", str(output)])
    assert inspected.exit_code == 0, inspected.output
    for text in (
        load_snapshot(output).scope.subscription_id,
        "Location: eastus; service: Microsoft.Compute",
        "Fact citations: /vm_size",
        "platform/scope/location/service",
        "Failure [FixtureUnavailable]",
    ):
        assert text in inspected.stdout


@pytest.mark.parametrize("status", ["redacted", "unresolved"])
def test_evidence_displays_value_limitations(tmp_path: Path, status: str) -> None:
    data = json.loads((FIXTURES / "partial-coverage.json").read_text())
    record = data["evidence"]["sg-rule"]
    if status == "redacted":
        record["value"] = {"Password": "private-value", "FromPort": 443}
    else:
        record.update(value=None, value_status=status, limitation="Source fact unavailable")
    operation = data["coverage"]["operations"][0]
    operation.update(
        status="incomplete",
        reason="Partial response",
        unresolved_values=[{"configuration_path": "VpcId", "reason": "Unknown expression"}],
    )
    source = tmp_path / "source.json"
    source.write_text(json.dumps(data))
    saved = tmp_path / "saved.json"
    assert runner.invoke(app, ["ingest", str(source), "-o", str(saved)]).exit_code == 0
    arguments = ["evidence", "sg-rule", "--snapshot", str(saved)]
    readable, result = (runner.invoke(app, [*arguments, *flags]) for flags in ([], ["--json"]))
    for outcome in (readable, result):
        assert outcome.exit_code == 0, outcome.output
        assert "private-value" not in outcome.output
        assert status in outcome.stdout
        assert "Partial response" in outcome.stdout
        assert "Unknown expression" in outcome.stdout
    assert "private-value" not in saved.read_text()
    evidence = json.loads(result.stdout)["evidence"]
    assert evidence["value_status"] == status
    assert evidence["redacted_paths"] == (["/Password"] if status == "redacted" else [])
    assert (
        "Redacted paths" if status == "redacted" else "Source fact unavailable"
    ) in readable.stdout


def test_readable_file_evidence_without_operation_coverage(tmp_path: Path) -> None:
    data = json.loads((FIXTURES / "cloudformation.json").read_text())
    snapshot = load_snapshot(FIXTURES / "cloudformation.json")
    resource = snapshot.regions["eu-west-1"].resources[0]
    data["evidence"] = {
        "declaration": {
            "snapshot_id": str(snapshot.snapshot_id),
            "resource_id": resource.id,
            "source": "cloudformation",
            "locator": "network.json",
            "configuration_path": "Resources.MainVpc.Properties.CidrBlock",
            "value": "10.0.0.0/16",
        }
    }
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(data))
    result = runner.invoke(app, ["evidence", "declaration", "--snapshot", str(source)])
    assert result.exit_code == 0, result.output
    assert "operation/file: network.json" in result.stdout
    assert "Resources.MainVpc.Properties.CidrBlock" in result.stdout
    assert '"10.0.0.0/16"' in result.stdout
    assert "No operation coverage recorded" in result.stdout
    assert "Fact citations: <none>" in result.stdout


def _unknown_owner(source: Path) -> None:
    """The partial-coverage fixture with a secret as the owner of its evidence."""
    data = json.loads((FIXTURES / "partial-coverage.json").read_text())
    data["evidence"]["sg-rule"]["resource_id"] = "never-disclose"
    source.write_text(json.dumps(data))


BAD_INPUTS = {
    "json": (lambda source: source.write_text('{"secret":"never-disclose",'), "invalid_json", 2),
    "reference": (_unknown_owner, "invalid_snapshot", 2),
    "missing": (lambda source: None, "snapshot_io_error", 1),
}


@pytest.mark.parametrize("kind", BAD_INPUTS)
def test_ingest_reports_safe_input_errors(tmp_path: Path, kind: str) -> None:
    write, code, exit_code = BAD_INPUTS[kind]
    source = tmp_path / "bad.json"
    write(source)
    output = tmp_path / "output.json"
    arguments = ["ingest", str(source), "-o", str(output)]
    readable = runner.invoke(app, arguments)
    result = runner.invoke(app, [*arguments, "--json"])
    for outcome in (readable, result):
        assert outcome.exit_code == exit_code, outcome.output
        assert "never-disclose" not in outcome.output
        assert "Traceback" not in outcome.output
    assert not output.exists()
    assert readable.stdout == ""
    assert code in readable.stderr and str(source) in readable.stderr
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["path"], result.stderr) == (code, str(source), "")
    if kind == "reference":
        assert error["details"]
    if kind == "json":
        assert error["line"] == 1 and error["column"] > 0


def test_missing_evidence_has_distinct_exit_code() -> None:
    result = runner.invoke(
        app, ["evidence", "absent", "--snapshot", str(FIXTURES / "hierarchy.json"), "--json"]
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "evidence_not_found"


def test_ingest_never_overwrites(tmp_path: Path) -> None:
    # Each kind of existing destination is the save's concern (services tests); the command
    # reports the conflict, leaves both files alone, and removes its temporary file.
    source = tmp_path / "source.json"
    source.write_bytes((FIXTURES / "hierarchy.json").read_bytes())
    output = tmp_path / "output.json"
    output.write_text("existing invalid JSON")
    original = source.read_bytes()
    result = runner.invoke(app, ["ingest", str(source), "-o", str(output), "--json"])
    assert result.exit_code == 3, result.output
    assert json.loads(result.stdout)["error"]["code"] == "snapshot_conflict"
    assert source.read_bytes() == original
    assert output.read_text() == "existing invalid JSON"
    assert not list(tmp_path.glob(".archdiscov-*.tmp"))
