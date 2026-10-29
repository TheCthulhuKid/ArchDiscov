import json

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.services import load_analysis, load_snapshot
from tests.paths import FIXTURES_ROOT

runner = CliRunner()
OWNERSHIP = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
AZURE = SNAPSHOTS / "azure.json"


def run(*arguments):
    return runner.invoke(app, list(arguments))


def analyze(snapshot, output, *extra):
    return run("analyze", "--snapshot", str(snapshot), "--output", str(output), *extra)


# The Azure fixture's limited coverage qualifies the findings, so the readable form names it.
@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_analyze_saves_a_result_that_findings_verifies(tmp_path, as_json):
    output = tmp_path / "result.json"
    flags = ["--json"] if as_json else []
    saved = analyze(AZURE, output, *flags)
    assert saved.exit_code == 0, saved.output
    result = load_analysis(output, snapshot=load_snapshot(AZURE))
    shown = run("findings", str(output), "--snapshot", str(AZURE), *flags)
    assert shown.exit_code == 0, shown.output
    if as_json:
        payload = json.loads(shown.stdout)
        assert json.loads(saved.stdout)["output_path"] == str(output)
        assert payload["result_path"] == str(output)
        assert payload["analyzer"] == "ownership"
        assert [item["id"] for item in payload["findings"]] == [f.id for f in result.findings]
    else:
        assert f"Findings: {len(result.findings)}" in shown.stdout
        assert "      coverage: Microsoft.Compute/" in shown.stdout
        assert "not proof" in shown.stdout
        assert str(result.snapshot_id) in shown.stdout


def test_ownership_keys_reach_the_analyzer(tmp_path):
    output = tmp_path / "custom.json"
    result = analyze(OWNERSHIP, output, "--ownership-key", "Name", "--ownership-key", "Squad")
    assert result.exit_code == 0, result.output
    assert "Findings: 0" in result.stdout
    assert '"ownership_keys":["Name","Squad"]' in result.stdout


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "result.json"
    output.write_text("precious", encoding="utf-8")
    result = analyze(OWNERSHIP, output)
    assert result.exit_code == 3
    assert "snapshot_conflict" in result.output
    assert output.read_text(encoding="utf-8") == "precious"
    assert sorted(tmp_path.iterdir()) == [output]


def test_invalid_requests_exit_safely_and_write_nothing(tmp_path):
    output = tmp_path / "result.json"
    keys = ["--ownership-key", "SECRET", "--ownership-key", "SECRET"]
    result = analyze(OWNERSHIP, output, "--json", *keys)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_parameters"
    assert "SECRET" not in result.output
    assert "Traceback" not in result.output
    assert not output.exists()


def test_findings_rejects_wrong_snapshot_and_bad_files(tmp_path):
    saved = tmp_path / "result.json"
    assert analyze(OWNERSHIP, saved).exit_code == 0
    mismatched = run(
        "findings", str(saved), "--snapshot", str(SNAPSHOTS / "network.json"), "--json"
    )
    assert mismatched.exit_code == 2
    error = json.loads(mismatched.stdout)["error"]
    assert error["code"] == "invalid_analysis_result"
    assert error["path"] == str(saved)

    missing = run("findings", str(tmp_path / "none.json"), "--snapshot", str(OWNERSHIP))
    assert missing.exit_code == 1
    assert "Traceback" not in missing.output

    tampered = json.loads(saved.read_text(encoding="utf-8"))
    tampered["findings"][0]["summary"] = "SECRET edit"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(tampered), encoding="utf-8")
    rejected = run("findings", str(edited), "--snapshot", str(OWNERSHIP))
    assert rejected.exit_code == 2
    assert "SECRET" not in rejected.output


@pytest.fixture
def group_snapshot(tmp_path):
    from archdiscov.services import save_snapshot, validate_snapshot
    from tests.support.security_group_data import variant

    return save_snapshot(validate_snapshot(variant()), tmp_path / "groups.json")


def group_arguments(snapshot, output, *extra):
    from tests.support.security_group_data import IDS

    return [
        "analyze",
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--analyzer",
        "security_group_impact",
        "--resource-id",
        IDS["sg-app"],
        *extra,
    ]


def test_security_group_findings_show_their_impact_and_depth(tmp_path, group_snapshot):
    output = tmp_path / "result.json"
    saved = run(*group_arguments(group_snapshot, output, "--max-depth", "2"))
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(group_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Impact: observed direct_attachment" in shown.stdout
    assert '"max_depth":2' in shown.stdout


@pytest.fixture
def trust_snapshot(tmp_path):
    from archdiscov.services import save_snapshot
    from tests.support.trust_data import OTHER, build, statement

    snapshot = build(
        {"web": [statement({"AWS": OTHER}, condition=True), statement({"AWS": "*"}, effect="Deny")]}
    )
    return save_snapshot(snapshot, tmp_path / "trust.json")


def trust_arguments(snapshot, output, *extra):
    return [
        "analyze",
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--analyzer",
        "cross_account_trust",
        *extra,
    ]


def test_trust_findings_show_classification_effect_and_conditionality(tmp_path, trust_snapshot):
    output = tmp_path / "result.json"
    saved = run(*trust_arguments(trust_snapshot, output, "--result-limit", "10"))
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(trust_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Trust: role_trust_policy external_account Allow [conditional]" in shown.stdout
    assert '"result_limit":10' in shown.stdout


@pytest.fixture
def database_snapshot(tmp_path):
    from archdiscov.services import save_snapshot
    from tests.support.source_data import build

    return save_snapshot(build(task={}, function={}), tmp_path / "database.json")


def database_arguments(snapshot, output, *extra):
    from archdiscov.models.enums import AwsService
    from tests.support.rds_data import INSTANCE
    from tests.support.source_data import rid

    return [
        "analyze",
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--analyzer",
        "database_access",
        "--resource-id",
        rid(INSTANCE, AwsService.RDS),
        *extra,
    ]


def test_database_access_findings_show_source_decision_and_elements(tmp_path, database_snapshot):
    output = tmp_path / "result.json"
    saved = run(*database_arguments(database_snapshot, output, "--port", "5432"))
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(database_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Database access: ec2_instance allow on port 5432 [source_egress allow" in shown.stdout
    assert '"port":5432' in shown.stdout


@pytest.fixture
def exposure_snapshot(tmp_path):
    from archdiscov.services import save_snapshot
    from tests.support.lb_exposure_data import FULL_COVERAGE, build, ec2_items

    snapshot = build(ec2=ec2_items(public_eni=True), coverage=FULL_COVERAGE)
    return save_snapshot(snapshot, tmp_path / "exposure.json")


def exposure_arguments(snapshot, output, *extra):
    return [
        "analyze",
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--analyzer",
        "internet_exposure",
        *extra,
    ]


def test_exposure_findings_show_entry_decision_elements_and_health(tmp_path, exposure_snapshot):
    output = tmp_path / "result.json"
    saved = run(
        *exposure_arguments(exposure_snapshot, output, "--max-depth", "2", "--result-limit", "9")
    )
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(exposure_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Internet exposure: load_balancer allow on tcp 80 [scheme allow" in shown.stdout
    assert "observed target health: unhealthy" in shown.stdout
    assert '"max_depth":2' in shown.stdout and '"result_limit":9' in shown.stdout
