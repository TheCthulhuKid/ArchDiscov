import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.models.cloud_snapshot import CloudSnapshot
from archdiscov.services import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"
runner = CliRunner()


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_ingest_preserves_every_fixture(tmp_path: Path, fixture: Path) -> None:
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
        assert payload["snapshot_id"] == "00000000-0000-4000-8000-000000000004"
        assert payload["snapshot_source"] == {
            "kind": "aws",
            "exported_at": None,
            "profile": "development",
            "role_arn": None,
        }
        assert payload["evidence_id"] == "sg-rule"
        assert payload["resource"]["id"] == payload["evidence"]["resource_id"]
        assert payload["evidence"]["locator"] == "DescribeSecurityGroups"
        assert payload["evidence"]["configuration_path"] == "SecurityGroups[0].IpPermissions[0]"
        assert payload["evidence"]["value"]["FromPort"] == 443
        assert payload["fact_paths"] == ["/attributes/ingress/0"]
        assert payload["coverage"][1]["failures"][0]["code"] == "UnauthorizedOperation"
        assert payload["coverage"][2]["result_count"] == 0
    else:
        assert "Saved snapshot" in imported.stdout
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


@pytest.mark.parametrize("status", ["redacted", "omitted", "unresolved"])
@pytest.mark.parametrize("json_output", [False, True])
def test_evidence_displays_value_limitations(
    tmp_path: Path, status: str, json_output: bool
) -> None:
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
    result = runner.invoke(
        app, ["evidence", "sg-rule", "--snapshot", str(saved), *(["--json"] if json_output else [])]
    )
    assert result.exit_code == 0, result.output
    assert "private-value" not in result.output
    assert "private-value" not in saved.read_text()
    assert status in result.stdout
    assert "Partial response" in result.stdout
    assert "Unknown expression" in result.stdout
    if json_output:
        evidence = json.loads(result.stdout)["evidence"]
        assert evidence["value_status"] == status
        assert evidence["redacted_paths"] == (["/Password"] if status == "redacted" else [])
    else:
        assert (
            "Redacted paths" if status == "redacted" else "Source fact unavailable"
        ) in result.stdout


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


@pytest.mark.parametrize(
    "kind,code,exit_code",
    [
        ("json", "invalid_json", 2),
        ("encoding", "invalid_encoding", 2),
        ("version", "unsupported_schema_version", 2),
        ("reference", "invalid_snapshot", 2),
        ("discriminator", "invalid_snapshot", 2),
        ("missing", "snapshot_io_error", 1),
        ("directory", "snapshot_io_error", 1),
    ],
)
@pytest.mark.parametrize("command", ["ingest", "evidence"])
@pytest.mark.parametrize("json_output", [False, True])
def test_commands_report_safe_input_errors(
    tmp_path: Path, kind: str, code: str, exit_code: int, command: str, json_output: bool
) -> None:
    source = tmp_path / "bad.json"
    if kind == "json":
        source.write_text('{"secret":"never-disclose",')
    elif kind == "encoding":
        source.write_bytes(b"\xff")
    elif kind == "directory":
        source.mkdir()
    elif kind != "missing":
        data = json.loads((FIXTURES / "partial-coverage.json").read_text())
        if kind == "version":
            data["schema_version"] = "never-disclose"
        elif kind == "reference":
            data["evidence"]["sg-rule"]["resource_id"] = "never-disclose"
        else:
            data["source"]["kind"] = "never-disclose"
        source.write_text(json.dumps(data))
    output = tmp_path / "output.json"
    arguments = (
        ["ingest", str(source), "-o", str(output)]
        if command == "ingest"
        else ["evidence", "sg-rule", "--snapshot", str(source)]
    )
    result = runner.invoke(app, [*arguments, *(["--json"] if json_output else [])])
    assert result.exit_code == exit_code, result.output
    assert "never-disclose" not in result.output
    assert "Traceback" not in result.output
    assert not output.exists()
    if json_output:
        error = json.loads(result.stdout)["error"]
        assert error["code"] == code
        assert error["path"] == str(source)
        assert result.stderr == ""
        if kind == "version":
            assert error["details"][0]["location"] == ["schema_version"]
            assert error["details"][0]["type"] == code
        if kind == "json":
            assert error["line"] == 1 and error["column"] > 0
    else:
        assert result.stdout == ""
        assert code in result.stderr
        assert str(source) in result.stderr


@pytest.mark.parametrize("json_output", [False, True])
def test_missing_evidence_has_distinct_exit_code(json_output: bool) -> None:
    result = runner.invoke(
        app,
        [
            "evidence",
            "absent",
            "--snapshot",
            str(FIXTURES / "hierarchy.json"),
            *(["--json"] if json_output else []),
        ],
    )
    assert result.exit_code == 4
    assert "evidence_not_found" in result.output
    if json_output:
        assert json.loads(result.stdout)["error"]["code"] == "evidence_not_found"


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "same-input"])
def test_ingest_never_overwrites(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source.json"
    source.write_bytes((FIXTURES / "hierarchy.json").read_bytes())
    output = tmp_path / "output.json"
    if kind == "file":
        output.write_text("existing invalid JSON")
    elif kind == "directory":
        output.mkdir()
    elif kind == "symlink":
        output.symlink_to(tmp_path / "absent.json")
    else:
        output = source
    original = source.read_bytes()
    result = runner.invoke(app, ["ingest", str(source), "-o", str(output), "--json"])
    assert result.exit_code == 3, result.output
    assert json.loads(result.stdout)["error"]["code"] == "snapshot_conflict"
    assert source.read_bytes() == original
    if kind == "file":
        assert output.read_text() == "existing invalid JSON"
    elif kind == "directory":
        assert output.is_dir()
    elif kind == "symlink":
        assert output.is_symlink() and not output.exists()
    assert not list(tmp_path.glob(".archdiscov-*.tmp"))


def test_ingest_does_not_create_parent_directories(tmp_path: Path) -> None:
    output = tmp_path / "missing" / "snapshot.json"
    result = runner.invoke(
        app, ["ingest", str(FIXTURES / "hierarchy.json"), "-o", str(output), "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["path"] == str(output)
    assert not output.parent.exists()


@pytest.mark.parametrize("command", ["ingest", "evidence"])
@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_command_help_and_usage(command: str, styled: bool, monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("TTY_COMPATIBLE", "1" if styled else "0")
    help_result = runner.invoke(app, [command, "--help"])
    assert help_result.exit_code == 0
    assert "--json" in unstyle(help_result.stdout)
    assert runner.invoke(app, [command]).exit_code == 2


@pytest.mark.parametrize("entry_point", ["script", "module"])
def test_installed_workflow_outside_repository(tmp_path: Path, entry_point: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = (
        [str(Path(sys.executable).with_name("archdiscov"))]
        if entry_point == "script"
        else [sys.executable, "-m", "archdiscov"]
    )
    output = tmp_path / "snapshot.json"
    imported = subprocess.run(
        [*command, "ingest", str(FIXTURES / "hierarchy.json"), "-o", str(output), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["path"] == str(output)
    inspected = subprocess.run(
        [*command, "evidence", "instance", "--snapshot", str(output), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["resource"]["instance_type"] == "t3.micro"
