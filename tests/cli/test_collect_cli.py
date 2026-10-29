"""The `collect` command: scope and limit options in process, and what one service collects
through the CLI; what each service's collection contains is its adapter's tests' concern."""

import json

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from archdiscov import cli_collection
from archdiscov.cli import app
from archdiscov.ingestion import aws as aws_module
from archdiscov.ingestion.requests import COLLECTABLE_SERVICES, RegionScope
from archdiscov.ingestion.service_catalog import REGIONAL_OPERATIONS
from archdiscov.models.enums import AwsService
from archdiscov.services import load_snapshot
from tests.support.aws_fakes import Session
from tests.support.iam_data import ACCOUNT, IamClient, IamSession, ec2_pages, responses


def arguments(path):
    return ["collect", "--account", f"dev:{ACCOUNT}", "-o", str(path), "--json"]


@pytest.mark.parametrize(
    "flags,services,scope,regions",
    [
        (["--all-services"], COLLECTABLE_SERVICES, RegionScope.ENABLED, ()),
        (
            ["--all-services", "--region", "us-west-2"],
            COLLECTABLE_SERVICES,
            RegionScope.EXPLICIT,
            ("us-west-2",),
        ),
        (["--region", "us-east-1"], {"ec2"}, RegionScope.EXPLICIT, ("us-east-1",)),
        (["--service", "iam"], {"iam"}, RegionScope.EXPLICIT, ()),
        (["--service", "lambda", "--region-scope", "enabled"], {"lambda"}, RegionScope.ENABLED, ()),
    ],
)
def test_cli_selects_scope_without_changing_selective_defaults(
    monkeypatch, tmp_path, flags, services, scope, regions
):
    seen = []
    monkeypatch.setattr(
        cli_collection, "collect_and_save", lambda request, paths: seen.append(request) or []
    )
    result = CliRunner().invoke(app, [*arguments(tmp_path / "snapshot.json"), *flags])
    assert result.exit_code == 0, result.output
    (request,) = seen
    (account,) = request.accounts
    assert set(account.services) == services
    assert account.region_scope is scope
    assert account.regions == regions
    assert account.all_services == ("--all-services" in flags)


def test_cli_forwards_execution_and_bootstrap_controls(monkeypatch, tmp_path):
    seen = []
    paths = []

    def collect(request, outputs):
        seen.append(request)
        paths.extend(outputs)
        return []

    monkeypatch.setattr(cli_collection, "collect_and_save", collect)
    output = tmp_path / "snapshot.json"
    result = CliRunner().invoke(
        app,
        [
            *arguments(output),
            "--all-services",
            "--bootstrap-region",
            "us-west-2",
            "--max-in-flight-calls",
            "7",
            "--account-max-in-flight-calls",
            "3",
            "--max-active-accounts",
            "1",
            "--max-collector-jobs",
            "5",
            "--max-concurrency",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    (request,) = seen
    assert paths == [output]
    assert request.max_in_flight_calls == 7
    assert request.max_active_accounts == 1
    assert request.max_collector_jobs == 5
    account = request.accounts[0]
    assert account.bootstrap_region == "us-west-2"
    assert account.max_in_flight_calls == 3
    assert account.max_concurrency == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--all-services", "--service", "ec2"],
        ["--all-services", "--region-scope", "explicit"],
        ["--all-services", "--region", "global"],
        ["--all-services", "--bootstrap-region", "invalid-private-value"],
        ["--all-services", "--max-in-flight-calls", "65"],
    ],
    ids=[
        "all-services-with-service",
        "explicit-scope-without-regions",
        "global-region",
        "unknown-bootstrap-region",
        "limit-out-of-range",
    ],
)
def test_invalid_scope_and_limits_are_rejected_before_collection(monkeypatch, tmp_path, flags):
    def unexpected(*args):
        pytest.fail("invalid CLI request reached collection")

    monkeypatch.setattr(cli_collection, "collect_and_save", unexpected)
    output = tmp_path / "not-created.json"
    result = CliRunner().invoke(app, [*arguments(output), *flags])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"]["code"] == "invalid_request"
    assert "invalid-private-value" not in result.output
    assert not output.exists()


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--all-services", "--max-concurrency", "0"], "max_concurrency"),
        (["--region", "invalid-private-value"], "--region"),
    ],
    ids=["model-issue-location", "option-message"],
)
def test_a_rejected_request_names_what_is_wrong_without_its_value(
    monkeypatch, tmp_path, flags, named
):
    monkeypatch.setattr(cli_collection, "collect_and_save", lambda *args: [])
    result = CliRunner().invoke(app, [*arguments(tmp_path / "x.json"), *flags])
    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    locations = [part for issue in error.get("details", []) for part in issue["location"]]
    assert named in error["message"] or named in locations
    assert "invalid-private-value" not in result.output


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
    assert len(snapshot.regions) == (2 if combined else 0)
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


# The default service, EC2, is regional; the request model's own rules are its tests' concern.
@pytest.mark.parametrize(
    "options",
    [[], ["--service", "not-a-service"]],
    ids=["default-service-without-region", "unknown-service"],
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
