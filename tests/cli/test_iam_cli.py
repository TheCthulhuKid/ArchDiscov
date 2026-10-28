import json

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws as aws_module
from archdiscov.ingestion.service_catalog import REGIONAL_OPERATIONS
from archdiscov.models.enums import AwsService
from archdiscov.services import load_snapshot
from tests.support.aws_fakes import Session
from tests.support.iam_data import ACCOUNT, IamClient, IamSession, ec2_pages, responses


@pytest.mark.parametrize("combined", [False, True])
def test_collect_services_then_inspect_evidence_without_overwrite(tmp_path, monkeypatch, combined):
    session = IamSession(ec2=Session(pages=ec2_pages))
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kwargs: session)
    runner = CliRunner()
    output = tmp_path / "iam.json"
    args = ["collect", "--account", f"dev:{ACCOUNT}", "--service", "iam", "-o", str(output)]
    if combined:
        args += ["--service", "ec2", "--region", "us-east-1", "--region", "us-west-2"]
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)["accounts"][0]
    assert summary["complete"] is True
    ec2_reads = len(REGIONAL_OPERATIONS[AwsService.EC2])
    assert len(summary["operations"]) == (2 * ec2_reads + 8 if combined else 8)
    snapshot = load_snapshot(output)
    assert len(snapshot.global_resources) == 3
    assert len(snapshot.regions) == (2 if combined else 0)
    assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
    role = next(
        resource
        for resource in snapshot.global_resources
        if resource.name == "web" and resource.resource_type == "AWS::IAM::Role"
    )
    evidence_id = role.fact_evidence["/trust_document"][0]
    inspected = runner.invoke(app, ["evidence", evidence_id, "--snapshot", str(output), "--json"])
    assert inspected.exit_code == 0, inspected.output
    data = json.loads(inspected.output)
    assert data["evidence"]["resource_id"] == role.id
    assert data["evidence"]["locator"].startswith("iam:")
    assert data["evidence"]["redacted_paths"]
    assert "protected-context" not in output.read_text()
    before = output.read_bytes()
    again = runner.invoke(app, [*args, "--json"])
    assert again.exit_code == 3
    assert json.loads(again.output)["accounts"][0]["error"]["code"] == "snapshot_conflict"
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--service", "ec2"],
        ["--service", "iam", "--service", "ec2"],
        ["--service", "not-a-service"],
        ["--service", "s3"],
        ["--service", "iam", "--service", "iam"],
    ],
)
def test_invalid_service_or_missing_required_region_makes_no_calls(tmp_path, monkeypatch, options):
    calls = []
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kw: calls.append(kw))
    output = tmp_path / "invalid.json"
    result = CliRunner().invoke(
        app, ["collect", "--account", f"dev:{ACCOUNT}", "-o", str(output), "--json", *options]
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"]["code"] == "invalid_request"
    assert not output.exists()
    assert calls == []


@pytest.mark.parametrize("json_output", [False, True])
def test_partial_iam_collection_is_explicit_and_sanitized(tmp_path, monkeypatch, json_output):
    data = responses()
    data["get_policy_version"] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "malicious-secret"}}, "GetPolicyVersion"
    )
    session = IamSession(IamClient(data))
    monkeypatch.setattr(aws_module, "default_session_factory", lambda **kwargs: session)
    output = tmp_path / "partial.json"
    args = ["collect", "--account", f"dev:{ACCOUNT}", "--service", "iam", "-o", str(output)]
    result = CliRunner().invoke(app, [*args, *(["--json"] if json_output else [])])
    assert result.exit_code == 0, result.output
    assert output.exists()
    if json_output:
        summary = json.loads(result.output)["accounts"][0]
        assert summary["complete"] is False
        version = next(op for op in summary["operations"] if op["operation"] == "GetPolicyVersion")
        assert version["status"] == "failed"
        assert version["failures"] == ["AccessDenied"]
    else:
        assert "PARTIAL" in result.output
        assert "GetPolicyVersion: failed" in result.output
    assert "malicious-secret" not in result.output + output.read_text()


def test_iam_accounts_remain_isolated_after_identity_failure(tmp_path, monkeypatch):
    sessions = []

    def factory(**kwargs):
        session = IamSession(account=ACCOUNT)
        sessions.append(session)
        return session

    monkeypatch.setattr(aws_module, "default_session_factory", factory)
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--account",
            "bad:111111111111",
            "--account",
            f"good:{ACCOUNT}",
            "--service",
            "iam",
            "-o",
            str(bad),
            "-o",
            str(good),
            "--json",
        ],
    )
    assert result.exit_code == 5, result.output
    failed, succeeded = json.loads(result.output)["accounts"]
    assert failed["error"]["code"] == "account_mismatch"
    assert succeeded["complete"] is True
    assert not bad.exists()
    assert load_snapshot(good).account_id == ACCOUNT
    assert sessions[0].iam.calls == []
