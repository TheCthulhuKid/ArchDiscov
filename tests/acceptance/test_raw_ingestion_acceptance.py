"""Offline installed-entrypoint journeys for raw IaC exports: one import per format, its
evidence inspected, and one refusal.

Snapshot JSON is `test_offline_acceptance.py`'s journey, and no overwrite and the readable
summaries are left to it and `tests/cli`.
"""

import json

import pytest

from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import Workspace

FIXTURES = FIXTURES_ROOT
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CONTEXT = ("--account-id", ACCOUNT, "--region", REGION)
STACK_ID = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/acceptance/stack-id"
SECRETS = (
    "masked-tag-marker",
    "tag-secret-marker",
    "userdata-secret-marker",
    "output-secret-marker",
    "never-copy-marker",
    "unsupported-secret-marker",
    "sensitive-plan-tag",
    "sensitive-before-tag",
    "plan-variable-secret",
    "plan-output-secret",
    "fixture-secret-must-not-persist",
    "parameter-secret-must-not-persist",
    "https://example.invalid",
)


@pytest.fixture
def offline_cli(tmp_path):
    """Run `ingest` or `evidence` with JSON output through the installed CLI, offline; no
    secret may reach its output."""
    ws = Workspace(tmp_path)

    def run(*arguments, exit_code=0):
        result = ws.run(*arguments, "--json", expect=exit_code)
        for secret in SECRETS:
            assert secret not in result.stdout + result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)

    yield run
    assert ws.guard.attempts == []


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


@pytest.mark.parametrize(
    "format_name,fixture_name",
    [
        ("terraform-state", "terraform/state.json"),
        ("terraform-plan", "terraform/plan.json"),
        ("cloudformation", "cloudformation/template.yaml"),
    ],
)
def test_import_save_load_and_evidence_journey(offline_cli, tmp_path, format_name, fixture_name):
    fixture = FIXTURES / fixture_name
    source = tmp_path / fixture.name
    source.write_bytes(fixture.read_bytes())
    source_bytes = source.read_bytes()
    options = ["--format", format_name, *CONTEXT]
    if format_name.startswith("terraform-"):
        options += ["--workspace", "acceptance"]
    elif format_name == "cloudformation":
        parameters = tmp_path / "parameters.json"
        parameters.write_text(
            json.dumps({"Stage": "qa", "Protected": "parameter-secret-must-not-persist"})
        )
        options += [
            "--stack-name",
            "acceptance",
            "--stack-id",
            STACK_ID,
            "--parameters-file",
            str(parameters),
        ]
    output = tmp_path / "saved.json"
    arguments = ["ingest", source, "--output", output, *options]
    imported = offline_cli(*arguments)
    snapshot = load_snapshot(output)
    assert (imported["snapshot_id"], imported["path"]) == (str(snapshot.snapshot_id), str(output))
    assert imported["source_kind"] == snapshot.source.kind
    assert snapshot.account_id == ACCOUNT
    assert REGION in snapshot.regions
    resources = list(flatten(snapshot.regions[REGION].resources))

    # A declaration is not an observation: its coverage is incomplete and it names its file.
    assert imported["complete"] is False
    assert snapshot.source.path == str(source)
    # Any cited fact; what each format maps to which fact is its ingestion tests' to prove.
    evidence_id = next(ids[0] for resource in resources for ids in resource.fact_evidence.values())
    saved_bytes = output.read_bytes()
    for secret in SECRETS:
        assert secret.encode() not in saved_bytes
    inspected = offline_cli("evidence", evidence_id, "--snapshot", output)
    assert inspected["evidence"] == snapshot.evidence[evidence_id].model_dump(mode="json")
    assert inspected["fact_paths"]
    for fact_path in inspected["fact_paths"]:
        assert evidence_id in inspected["resource"]["fact_evidence"][fact_path]

    assert source.read_bytes() == fixture.read_bytes() == source_bytes
    assert output.read_bytes() == saved_bytes


def test_malformed_input_is_safe_and_creates_no_output(offline_cli, tmp_path):
    # One format: each parser's refusals are its ingestion tests' and the import service's.
    source = tmp_path / "malformed.json"
    source.write_text('{"parameter-secret-must-not-persist": [')
    output = tmp_path / "not-created.json"
    result = offline_cli(
        "ingest", source, "--output", output, "--format", "cloudformation", *CONTEXT, exit_code=2
    )
    assert result["error"]["code"]
    assert result["error"]["message"]
    assert not output.exists()
    assert source.read_text() == '{"parameter-secret-must-not-persist": ['
