import json

import pytest
from click import unstyle
from typer.testing import CliRunner

from archdiscov import cli, cli_collection
from archdiscov.ingestion.requests import COLLECTABLE_SERVICES, RegionScope
from tests.support.installed_cli import run_installed
from tests.support.offline_guard import OfflineGuard

ACCOUNT = "123456789012"


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
    result = CliRunner().invoke(cli.app, [*arguments(tmp_path / "snapshot.json"), *flags])
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
        cli.app,
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
)
def test_invalid_scope_and_limits_are_rejected_before_collection(monkeypatch, tmp_path, flags):
    def unexpected(*args):
        pytest.fail("invalid CLI request reached collection")

    monkeypatch.setattr(cli_collection, "collect_and_save", unexpected)
    output = tmp_path / "not-created.json"
    result = CliRunner().invoke(cli.app, [*arguments(output), *flags])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"]["code"] == "invalid_request"
    assert "invalid-private-value" not in result.output
    assert not output.exists()


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--all-services", "--max-concurrency", "0"], "max_concurrency"),
        (["--all-services", "--max-collector-jobs", "65"], "max_collector_jobs"),
        (["--region", "invalid-private-value"], "--region"),
        (["--service", "invalid-private-value"], "--service"),
        (["--all-services", "--service", "ec2"], "--all-services"),
    ],
)
def test_a_rejected_request_names_what_is_wrong_without_its_value(
    monkeypatch, tmp_path, flags, named
):
    monkeypatch.setattr(cli_collection, "collect_and_save", lambda *args: [])
    result = CliRunner().invoke(cli.app, [*arguments(tmp_path / "x.json"), *flags])
    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    locations = [part for issue in error.get("details", []) for part in issue["location"]]
    assert named in error["message"] or named in locations
    assert "invalid-private-value" not in result.output


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_installed_whole_architecture_controls_outside_checkout(tmp_path, styled):
    env = OfflineGuard(tmp_path).env
    env.update(TERM="xterm", TTY_COMPATIBLE="1" if styled else "0")
    help_text = unstyle(run_installed(tmp_path, "collect", "--help", env=env).stdout)
    for flag in (
        "--all-services",
        "--region-scope",
        "--max-in-flight-calls",
        "--max-active-accounts",
    ):
        assert flag in help_text
    output = tmp_path / "not-created.json"
    result = run_installed(
        tmp_path, *arguments(output), "--all-services", "--service", "iam", expect=2, env=env
    )
    assert json.loads(result.stdout)["error"]["code"] == "invalid_request"
    assert not output.exists()
