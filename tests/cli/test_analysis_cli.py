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


def test_ownership_keys_are_configurable_and_findings_cite_evidence(tmp_path):
    default = tmp_path / "default.json"
    assert analyze(OWNERSHIP, default, "--json").exit_code == 0
    [finding] = json.loads(
        run("findings", str(default), "--snapshot", str(OWNERSHIP), "--json").stdout
    )["findings"]
    assert finding["status"] == "supported"
    assert finding["evidence_ids"] == ["tags"]
    evidence = run("evidence", "tags", "--snapshot", str(OWNERSHIP), "--json")
    assert evidence.exit_code == 0, evidence.output
    assert json.loads(evidence.stdout)["resource"]["id"] == finding["resources"][0]["resource_id"]

    custom = tmp_path / "custom.json"
    result = analyze(OWNERSHIP, custom, "--ownership-key", "Name", "--ownership-key", "Squad")
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
        (["--analyzer", "security_group_impact"], "unsupported_analyzer"),
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
