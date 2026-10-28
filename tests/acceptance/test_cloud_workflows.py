import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.graph import GraphIndex
from archdiscov.models.cloud.contracts import CloudEvidence
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.services import (
    SnapshotConflictError,
    SnapshotValidationError,
    get_evidence,
    ingest_snapshot,
    load_snapshot,
    save_snapshot,
    upgrade_snapshot,
    validate_snapshot,
)
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import Workspace

FIXTURES = FIXTURES_ROOT / "snapshots"
runner = CliRunner()


def vm(snapshot: CloudSnapshot):
    return snapshot.regions["eastus"].resources[0].children[0].children[0]


def expected_operations(snapshot: CloudSnapshot) -> list[dict]:
    return [
        {
            "platform": operation.scope.platform.value,
            "scope": operation.scope.model_dump(mode="json"),
            "location": operation.location,
            "service": operation.service,
            "operation": operation.operation,
            "status": operation.status.value,
            "result_count": operation.result_count,
            "failures": [failure.code for failure in operation.failures],
        }
        for operation in snapshot.coverage.operations
    ]


def test_azure_public_validation_persistence_and_default_import(tmp_path: Path) -> None:
    source = FIXTURES / "azure.json"
    original = source.read_bytes()
    snapshot = load_snapshot(source)
    assert isinstance(snapshot, CloudSnapshot)
    assert validate_snapshot(json.loads(original)) == snapshot
    saved = save_snapshot(snapshot, tmp_path / "saved.json")
    assert load_snapshot(saved) == snapshot
    assert saved.stat().st_mode & 0o777 == 0o600
    result = ingest_snapshot(saved, tmp_path / "imported.json")
    assert result.snapshot == snapshot
    assert result.complete is False
    assert result.path == tmp_path / "imported.json"
    assert load_snapshot(result.path) == snapshot
    assert source.read_bytes() == original
    assert json.loads(result.path.read_text())["schema_version"] == "3"


@pytest.mark.parametrize("input_kind", ["mapping", "model"])
def test_azure_validation_and_save_redact_without_mutating_caller(
    tmp_path: Path, input_kind: str
) -> None:
    snapshot = load_snapshot(FIXTURES / "azure.json")
    vm(snapshot).attributes["password"] = "protected-resource-marker"
    snapshot.evidence["vm-attachment"].value["token"] = "protected-evidence-marker"
    data = snapshot if input_kind == "model" else snapshot.model_dump(mode="python")
    validated = validate_snapshot(data)
    assert vm(validated).attributes["password"] == "[REDACTED]"
    assert validated.evidence["vm-attachment"].value["token"] == "[REDACTED]"
    saved = save_snapshot(data, tmp_path / "saved.json")
    assert b"protected-resource-marker" not in saved.read_bytes()
    assert b"protected-evidence-marker" not in saved.read_bytes()
    assert load_snapshot(saved) == validated
    assert vm(snapshot).attributes["password"] == "protected-resource-marker"
    assert snapshot.evidence["vm-attachment"].value["token"] == "protected-evidence-marker"
    if input_kind == "mapping":
        assert data["evidence"]["vm-attachment"]["value"]["token"] == "protected-evidence-marker"
    stored = saved.read_bytes()
    vm(validated).attributes.clear()
    assert saved.read_bytes() == stored


def test_azure_evidence_exact_fact_and_matching_coverage_context() -> None:
    snapshot = load_snapshot(FIXTURES / "azure.json")
    snapshot.coverage.locations.append("westus")
    unrelated = snapshot.coverage.operations[0].model_copy(deep=True)
    unrelated.location = "westus"
    unrelated.service = "Microsoft.Compute"
    snapshot.coverage.operations.append(unrelated)
    vm(snapshot).attributes["password"] = "protected-resource-marker"
    result = get_evidence(snapshot, "vm-size")
    assert isinstance(result.evidence, CloudEvidence)
    assert result.snapshot_source == snapshot.source
    assert result.resource.id == vm(snapshot).id
    assert result.fact_paths == ["/vm_size"]
    assert result.evidence.value is None
    assert result.evidence.value_status == "present"
    assert result.evidence.configuration_path == (
        "virtualMachines[0].properties.hardwareProfile.vmSize"
    )
    assert result.evidence.scope == snapshot.scope
    assert result.evidence.location == "eastus"
    assert result.evidence.service == "Microsoft.Compute"
    assert [operation.operation for operation in result.coverage] == [
        "fixture.virtual-machines",
        "fixture.instance-view",
    ]
    for operation in result.coverage:
        assert operation.scope == snapshot.scope
        assert operation.scope.platform == "azure"
        assert operation.location == "eastus"
        assert operation.service == "Microsoft.Compute"
    assert result.coverage[0].result_count == 1
    assert result.coverage[0].unresolved_values[0].resource_id == result.resource.id
    assert result.coverage[1].failures[0].code == "FixtureUnavailable"
    assert result.resource.attributes["password"] == "[REDACTED]"
    result.resource.attributes.clear()
    assert vm(snapshot).attributes["password"] == "protected-resource-marker"
    network = get_evidence(snapshot, "network-address")
    assert network.fact_paths == ["/address_spaces/0"]
    assert [(operation.status, operation.result_count) for operation in network.coverage] == [
        ("success", 0)
    ]
    unavailable = get_evidence(snapshot, "vm-unavailable")
    assert unavailable.fact_paths == []
    assert unavailable.evidence.value_status == "unresolved"
    assert unavailable.evidence.limitation == "Offline fixture does not provide this fact"


@pytest.mark.parametrize("mutation", ["id", "fact", "endpoint", "scope", "typed_value"])
def test_azure_mutation_rejected_without_publishing(tmp_path: Path, mutation: str) -> None:
    snapshot = load_snapshot(FIXTURES / "azure.json")
    if mutation == "id":
        vm(snapshot).id = "changed-id"
    elif mutation == "fact":
        vm(snapshot).fact_evidence["/absent"] = ["vm-size"]
    elif mutation == "endpoint":
        snapshot.dependencies[0].target.resource_id = "absent"
    elif mutation == "scope":
        snapshot.scope.subscription_id = "22222222-2222-2222-2222-222222222222"
    else:
        vm(snapshot).vm_size = 123
    with pytest.raises(SnapshotValidationError):
        save_snapshot(snapshot, tmp_path / "invalid.json")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("conflict", ["save", "ingest", "same_input"])
def test_azure_conflicts_preserve_existing_bytes(tmp_path: Path, conflict: str) -> None:
    source = tmp_path / "source.json"
    source.write_bytes((FIXTURES / "azure.json").read_bytes())
    output = source if conflict == "same_input" else tmp_path / "output.json"
    if output != source:
        output.write_bytes(b"existing bytes, not JSON")
    original_source, original_output = source.read_bytes(), output.read_bytes()
    with pytest.raises(SnapshotConflictError):
        if conflict == "save":
            save_snapshot(load_snapshot(source), output)
        else:
            ingest_snapshot(source, output)
    assert source.read_bytes() == original_source
    assert output.read_bytes() == original_output
    assert not list(tmp_path.glob(".archdiscov-*.tmp"))


@pytest.mark.parametrize("json_output", [False, True])
def test_azure_cli_ingest_and_evidence_context(tmp_path: Path, json_output: bool) -> None:
    snapshot = load_snapshot(FIXTURES / "azure.json")
    output = tmp_path / "snapshot.json"
    flags = ["--json"] if json_output else []
    imported = runner.invoke(
        app, ["ingest", str(FIXTURES / "azure.json"), "-o", str(output), *flags]
    )
    assert imported.exit_code == 0, imported.output
    stored = output.read_bytes()
    inspected = runner.invoke(app, ["evidence", "vm-size", "--snapshot", str(output), *flags])
    assert inspected.exit_code == 0, inspected.output
    if json_output:
        summary = json.loads(imported.stdout)
        assert summary["snapshot_id"] == str(snapshot.snapshot_id)
        assert summary["source_kind"] == "azure_fixture"
        assert summary["complete"] is False
        assert summary["operations"] == expected_operations(snapshot)
        payload = json.loads(inspected.stdout)
        assert payload["snapshot_source"] == snapshot.source.model_dump(mode="json")
        assert payload["resource"]["identity"]["platform"] == "azure"
        assert payload["resource"]["vm_size"] is None
        assert payload["fact_paths"] == ["/vm_size"]
        assert payload["evidence"]["scope"] == snapshot.scope.model_dump(mode="json")
        assert payload["evidence"]["location"] == "eastus"
        assert payload["evidence"]["service"] == "Microsoft.Compute"
        assert payload["evidence"]["value_status"] == "present"
        assert payload["evidence"]["value"] is None
        assert [operation["location"] for operation in payload["coverage"]] == ["eastus", "eastus"]
        assert "region" not in payload["resource"]["identity"]
    else:
        for text in ("azure", "eastus", "Microsoft.Compute", "FixtureUnavailable"):
            assert text in imported.stdout
        for text in (
            snapshot.scope.subscription_id,
            "Location: eastus; service: Microsoft.Compute",
            "platform/scope/location/service",
            "/vm_size",
            "FixtureUnavailable",
            "not a completeness guarantee",
        ):
            assert text in inspected.stdout
    assert output.read_bytes() == stored


def test_aws_explicit_schema3_upgrade_save_load_and_ingest(tmp_path: Path) -> None:
    legacy = load_snapshot(FIXTURES / "hierarchy.json")
    upgraded = upgrade_snapshot(legacy)
    assert isinstance(upgraded, CloudSnapshot)
    assert upgraded.schema_version == "3"
    assert upgraded.scope.platform == "aws"
    assert upgraded.scope.account_id == legacy.account_id
    assert upgraded.regions["eu-west-1"].resources == legacy.regions["eu-west-1"].resources
    source = save_snapshot(upgraded, tmp_path / "upgraded.json")
    result = ingest_snapshot(source, tmp_path / "imported.json")
    assert load_snapshot(result.path) == upgraded
    assert result.snapshot == upgraded
    evidence = get_evidence(result.snapshot, "instance")
    assert evidence.resource.id == "123456789012/eu-west-1/ec2/physical/i-web"
    assert evidence.fact_paths == ["/instance_type"]
    assert evidence.evidence.scope == upgraded.scope
    assert evidence.evidence.location == "eu-west-1"
    assert evidence.evidence.service == "ec2"


def test_installed_azure_workflow_outside_checkout_without_credentials(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    output = ws.path("snapshots", "azure.json")
    summary = ws.json("ingest", FIXTURES / "azure.json", "-o", output)
    assert summary["source_kind"] == "azure_fixture"
    assert summary["operations"] == expected_operations(load_snapshot(output))
    stored = output.read_bytes()
    payload = ws.json("evidence", "vm-size", "--snapshot", output)
    assert payload["fact_paths"] == ["/vm_size"]
    assert payload["evidence"]["scope"]["platform"] == "azure"
    assert payload["evidence"]["location"] == "eastus"
    assert payload["resource"]["vm_size"] is None
    assert output.read_bytes() == stored
    assert ws.guard.attempts == []


# The AWS-only analyzers over the Azure fixture: whether the analyzer selects a resource, its
# other options, the finding block that stays empty, and what the unsupported limitation
# names. `analyze` prints the findings it saved; the AWS journeys show `findings` prints them too.
UNSUPPORTED = {
    "security_group_impact": (True, [], "impact", "AWS"),
    "cross_account_trust": (False, [], "trust", "IAM trust policies"),
    "database_access": (True, ["--port", "5432"], "database_access", "database access"),
    "internet_exposure": (False, [], "internet_exposure", "internet exposure"),
}


@pytest.mark.parametrize("kind", UNSUPPORTED)
def test_installed_aws_only_analyzers_are_unsupported_on_azure(tmp_path: Path, kind: str) -> None:
    selects, options, block, named = UNSUPPORTED[kind]
    azure = FIXTURES / "azure.json"
    if selects:
        options = ["--resource-id", GraphIndex(load_snapshot(azure)).resource_ids[0], *options]
    ws = Workspace(tmp_path)
    output = ws.path("results", "result.json")
    saved = ws.json(
        "analyze", "--snapshot", azure, "--output", output, "--analyzer", kind, *options
    )
    [finding] = saved["findings"]
    assert finding["status"] == "insufficient_evidence"
    assert finding[block] is None
    assert finding["limitations"][0]["code"] == "unsupported_platform"
    assert named in finding["limitations"][0]["message"]
