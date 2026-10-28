import json

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.services import load_analysis, load_snapshot
from tests.paths import FIXTURES_ROOT

runner = CliRunner()
OWNERSHIP = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
SOURCES = [
    OWNERSHIP,
    SNAPSHOTS / "hierarchy.json",
    SNAPSHOTS / "azure.json",
    SNAPSHOTS / "network.json",
]


def run(*arguments):
    return runner.invoke(app, list(arguments))


def analyze(snapshot, output, *extra):
    return run("analyze", "--snapshot", str(snapshot), "--output", str(output), *extra)


@pytest.mark.parametrize("snapshot", SOURCES, ids=lambda path: path.stem)
@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_analyze_saves_a_result_that_findings_verifies(tmp_path, snapshot, as_json):
    output = tmp_path / "result.json"
    flags = ["--json"] if as_json else []
    saved = analyze(snapshot, output, *flags)
    assert saved.exit_code == 0, saved.output
    result = load_analysis(output, snapshot=load_snapshot(snapshot))
    shown = run("findings", str(output), "--snapshot", str(snapshot), *flags)
    assert shown.exit_code == 0, shown.output
    if as_json:
        payload = json.loads(shown.stdout)
        assert json.loads(saved.stdout)["output_path"] == str(output)
        assert payload["result_path"] == str(output)
        assert payload["analyzer"] == "ownership"
        assert [item["id"] for item in payload["findings"]] == [f.id for f in result.findings]
    else:
        assert f"Findings: {len(result.findings)}" in shown.stdout
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
    for flags in ([], ["--json"]):
        result = analyze(OWNERSHIP, output, *flags)
        assert result.exit_code == 3
        assert "snapshot_conflict" in result.output
    assert output.read_text(encoding="utf-8") == "precious"
    assert sorted(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (["--ownership-key", ""], "invalid_parameters"),
        (["--ownership-key", "SECRET", "--ownership-key", "SECRET"], "invalid_parameters"),
    ],
)
def test_invalid_requests_exit_safely_and_write_nothing(tmp_path, extra, code):
    output = tmp_path / "result.json"
    result = analyze(OWNERSHIP, output, "--json", *extra)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == code
    assert "SECRET" not in result.output
    assert "Traceback" not in result.output
    assert not output.exists()


def test_unknown_analyzer_is_a_usage_error(tmp_path):
    result = analyze(OWNERSHIP, tmp_path / "result.json", "--analyzer", "nonsense")
    assert result.exit_code == 2
    assert not (tmp_path / "result.json").exists()


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
    assert error["details"][0]["type"] == "snapshot_mismatch"

    missing = run("findings", str(tmp_path / "none.json"), "--snapshot", str(OWNERSHIP))
    assert missing.exit_code == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert run("findings", str(broken), "--snapshot", str(OWNERSHIP)).exit_code == 2
    no_snapshot = run("findings", str(saved), "--snapshot", str(tmp_path / "none.json"))
    assert no_snapshot.exit_code == 1
    assert "Traceback" not in no_snapshot.output

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


def group_arguments(snapshot, output, group="sg-app", *extra):
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
        IDS.get(group, group),
        *extra,
    ]


def test_security_group_findings_show_observed_and_potential_impact(tmp_path, group_snapshot):
    output = tmp_path / "result.json"
    saved = run(*group_arguments(group_snapshot, output, "sg-app", "--max-depth", "2"))
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(group_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Impact: observed direct_attachment" in shown.stdout
    assert "Impact: observed rule_reference" in shown.stdout
    assert "ingress; other_references_selected" in shown.stdout
    assert "Impact: potential downstream_dependency" in shown.stdout
    assert '"max_depth":2' in shown.stdout


def test_missing_resource_id_and_bad_selections_are_safe_errors(tmp_path, group_snapshot):
    output = tmp_path / "result.json"
    plain = [
        "analyze",
        "--snapshot",
        str(group_snapshot),
        "--output",
        str(output),
        "--analyzer",
        "security_group_impact",
        "--json",
    ]
    assert json.loads(run(*plain).stdout)["error"]["code"] == "invalid_parameters"
    missing = run(*group_arguments(group_snapshot, output, "absent"), "--json")
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["error"]["code"] == "resource_not_found"
    not_group = run(*group_arguments(group_snapshot, output, "instance"), "--json")
    assert json.loads(not_group.stdout)["error"]["code"] == "unsupported_resource"
    assert not output.exists()


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
    assert "Trust: role_trust_policy wildcard Deny" in shown.stdout
    assert "not effective access" in shown.stdout
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


def test_database_access_findings_show_source_decision_elements_and_blockers(
    tmp_path, database_snapshot
):
    output = tmp_path / "result.json"
    saved = run(*database_arguments(database_snapshot, output, "--port", "5432"))
    assert saved.exit_code == 0, saved.output
    shown = run("findings", str(output), "--snapshot", str(database_snapshot))
    assert shown.exit_code == 0, shown.output
    assert "Database access: ec2_instance allow on port 5432 [source_egress allow" in shown.stdout
    assert "Database access: ec2_instance unknown on port 5432" in shown.stdout
    assert "(missing_evidence)" in shown.stdout
    assert "not authentication" in shown.stdout
    result = load_analysis(output, snapshot=load_snapshot(database_snapshot))
    assert result.parameters.port == 5432


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
    assert "Internet exposure: public_address allow on tcp 80" in shown.stdout
    assert "not proof of reachability" in shown.stdout
    result = load_analysis(output, snapshot=load_snapshot(exposure_snapshot))
    assert (result.parameters.max_depth, result.parameters.result_limit) == (2, 9)


# One option each analyzer does not take or takes out of range, over the snapshot it analyzes;
# the parameter models prove the rest (`tests/analysis/test_analysis_models.py`).
REJECTED_OPTIONS = {
    "ownership": (None, ["--resource-id", "sg"]),
    "security_group_impact": ("group_snapshot", ["--max-depth", "11"]),
    "cross_account_trust": ("trust_snapshot", ["--max-depth", "2"]),
    "database_access": ("database_snapshot", []),
    "internet_exposure": ("exposure_snapshot", ["--port", "80"]),
}
BUILDERS = {
    "security_group_impact": group_arguments,
    "cross_account_trust": trust_arguments,
    "database_access": database_arguments,
    "internet_exposure": exposure_arguments,
}


@pytest.mark.parametrize("kind", REJECTED_OPTIONS)
def test_foreign_or_out_of_range_options_exit_2_and_write_nothing(request, tmp_path, kind):
    fixture, extra = REJECTED_OPTIONS[kind]
    output = tmp_path / "result.json"
    if fixture is None:
        result = analyze(OWNERSHIP, output, *extra, "--json")
    else:
        snapshot = request.getfixturevalue(fixture)
        result = run(*BUILDERS[kind](snapshot, output), *extra, "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_parameters"
    assert not output.exists()
