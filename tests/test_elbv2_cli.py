import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws as aws_module
from archdiscov.ingestion.elbv2.relationships import walk_resources
from archdiscov.models.load_balancing import Listener
from archdiscov.services import load_snapshot

from .elbv2_data import ACCOUNT, Elbv2Client, Elbv2Session, responses


def args(output):
    return [
        "collect",
        "--account",
        f"dev:{ACCOUNT}",
        "--service",
        "elasticloadbalancing",
        "--region",
        "us-east-1",
        "-o",
        str(output),
    ]


def test_cli_collection_evidence_inspection_and_no_overwrite(tmp_path, monkeypatch):
    session = Elbv2Session()
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kw: session)
    output = tmp_path / "elb.json"
    runner = CliRunner()
    result = runner.invoke(app, [*args(output), "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)["accounts"][0]
    assert summary["complete"] is True
    assert len(summary["operations"]) == 6
    assert all(op["service"] == "elasticloadbalancing" for op in summary["operations"])
    snap = load_snapshot(output)
    listener = next(
        r for r in walk_resources(snap.regions["us-east-1"].resources) if isinstance(r, Listener)
    )
    evidence_id = listener.fact_evidence["/actions/0/forward_groups/0/arn"][0]
    inspected = runner.invoke(app, ["evidence", evidence_id, "--snapshot", str(output), "--json"])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["evidence"]["configuration_path"].endswith(
        "ForwardConfig.TargetGroups[0].TargetGroupArn"
    )
    before = output.read_bytes()
    again = runner.invoke(app, [*args(output), "--json"])
    assert again.exit_code == 3
    assert output.read_bytes() == before


@pytest.mark.parametrize("json_output", [False, True])
def test_partial_summary_is_service_qualified_and_safe(tmp_path, monkeypatch, json_output):
    data = responses()
    data["describe_listeners"] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "protected-secret"}}, "DescribeListeners"
    )
    session = Elbv2Session(Elbv2Client(data))
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kw: session)
    output = tmp_path / "partial.json"
    result = CliRunner().invoke(app, [*args(output), *(["--json"] if json_output else [])])
    assert result.exit_code == 0, result.output
    if json_output:
        assert not json.loads(result.output)["accounts"][0]["complete"]
    else:
        assert "PARTIAL" in result.output
        assert "elasticloadbalancing DescribeListeners: failed" in result.output
    assert "protected-secret" not in result.output + output.read_text()


def test_elb_only_requires_regions_before_creating_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kw: calls.append(kw))
    output = tmp_path / "invalid.json"
    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--service",
            "elasticloadbalancing",
            "-o",
            str(output),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["error"]["code"] == "invalid_request"
    assert calls == []
    assert not output.exists()


def test_account_identity_failure_does_not_block_other_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kw: Elbv2Session())
    bad, good = tmp_path / "bad.json", tmp_path / "good.json"
    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--account",
            "bad:111111111111",
            "--account",
            f"good:{ACCOUNT}",
            "--service",
            "elasticloadbalancing",
            "--region",
            "us-east-1",
            "-o",
            str(bad),
            "-o",
            str(good),
            "--json",
        ],
    )
    assert result.exit_code == 5
    failed, succeeded = json.loads(result.output)["accounts"]
    assert failed["error"]["code"] == "account_mismatch"
    assert succeeded["complete"]
    assert not bad.exists()
    assert load_snapshot(good).account_id == ACCOUNT


def test_installed_entrypoint_help_and_region_validation_outside_checkout(tmp_path):
    executable = Path(sys.executable).parent / "archdiscov"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("AWS_")
    }
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    help_result = subprocess.run(
        [str(executable), "collect", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert help_result.returncode == 0
    assert "elasticloadbalancing" in help_result.stdout
    result = subprocess.run(
        [
            str(executable),
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--service",
            "elasticloadbalancing",
            "-o",
            str(tmp_path / "not-created.json"),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_request"
    assert not (tmp_path / "not-created.json").exists()
