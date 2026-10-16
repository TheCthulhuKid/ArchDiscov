import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.terraform import TerraformStateAdapter, TerraformStateRequest
from archdiscov.models.architecture import Ec2Instance, Subnet, UnknownResource, Vpc
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from archdiscov.services import (
    SnapshotConflictError,
    collect_snapshots,
    collect_snapshots_async,
    get_evidence,
    load_snapshot,
    save_snapshot,
)
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "terraform" / "state.json"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = AdapterContext(clock=lambda: NOW, uuid_factory=lambda: UUID(int=18))


def export_resource(resource_type="aws_instance", **values):
    return {
        "address": f"{resource_type}.example",
        "mode": "managed",
        "type": resource_type,
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "values": values,
        "sensitive_values": {},
    }


def write_export(tmp_path, resources, **extra):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"format_version": "1.0", "values": {"root_module": {"resources": resources}}, **extra}
        )
    )
    return path


def collect(path, **kwargs):
    return collect_snapshots(TerraformStateRequest(path=path, **kwargs), context=CONTEXT)[0]


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def test_nested_export_to_persistence_and_exact_evidence(tmp_path):
    snapshot = collect(FIXTURE, account_id="123456789012", region="eu-west-1", workspace="example")
    vpc = snapshot.regions["eu-west-1"].resources[0]
    subnet = vpc.children[0]
    instance = subnet.children[0]
    assert isinstance(vpc, Vpc) and isinstance(subnet, Subnet) and isinstance(instance, Ec2Instance)
    assert str(vpc.cidr_blocks[0]) == "10.0.0.0/16"
    assert subnet.availability_zone == "eu-west-1a"
    assert instance.instance_type == "t3.micro"
    assert instance.identity.logical_address == 'module.app.aws_instance.web["blue/green"]'
    assert instance.identity.physical_id == "i-state"
    assert snapshot.source.kind == "terraform_state"
    assert snapshot.source.workspace == "example"
    assert snapshot.source.terraform_version == "1.11.4"
    citation = instance.fact_evidence["/instance_type"][0]
    record = snapshot.evidence[citation]
    assert (
        record.configuration_path
        == "/values/root_module/child_modules/0/resources/1/values/instance_type"
    )
    assert record.value == "t3.micro" and record.source == "terraform"
    unknown = snapshot.regions["eu-west-1"].resources[1]
    assert isinstance(unknown, UnknownResource)
    assert unknown.aws_resource_type == "AWS::S3::Bucket"
    assert unknown.identity.service == AwsService.S3
    assert any(op.status == CoverageStatus.INCOMPLETE for op in snapshot.coverage.operations)
    deps = snapshot.dependencies_by_region["eu-west-1"]
    assert any(
        dep.source_id == subnet.id
        and dep.target.kind == "local"
        and dep.target.resource_id == vpc.id
        for dep in deps
    )
    assert any(
        dep.source_id == instance.id
        and dep.target.kind == "local"
        and dep.target.resource_id == subnet.id
        for dep in deps
    )
    unresolved = next(dep for dep in deps if dep.target.kind == "unresolved")
    assert unresolved.target.source_path.endswith("/vpc_security_group_ids/0")
    destination = save_snapshot(snapshot, tmp_path / "snapshot.json")
    assert load_snapshot(destination) == snapshot
    assert get_evidence(load_snapshot(destination), citation).evidence == record
    saved = destination.read_text()
    for secret in (
        "masked-tag-marker",
        "tag-secret-marker",
        "userdata-secret-marker",
        "output-secret-marker",
        "never-copy-marker",
        "unsupported-secret-marker",
    ):
        assert secret not in saved
    assert any(item.value_status == "redacted" for item in snapshot.evidence.values())
    before = destination.read_bytes()
    with pytest.raises(SnapshotConflictError):
        save_snapshot(snapshot, destination)
    assert destination.read_bytes() == before


def test_async_service_and_deterministic_identity(tmp_path):
    path = write_export(tmp_path, [export_resource(id="i-one", instance_type=None)])
    request = TerraformStateRequest(path=path, account_id="123456789012", region="eu-west-1")
    first = asyncio.run(collect_snapshots_async(request, context=CONTEXT))[0]
    second = collect(path, account_id="123456789012", region="eu-west-1")
    assert first == second
    instance = first.regions["eu-west-1"].resources[0]
    assert instance.instance_type is None
    assert first.evidence[instance.fact_evidence["/instance_type"][0]].value is None


@pytest.mark.parametrize("resource_type", ["aws_instance", "aws_iam_role"])
def test_logical_only_identity_and_global_scope(tmp_path, resource_type):
    path = write_export(tmp_path, [export_resource(resource_type)])
    snapshot = collect(path, account_id="123456789012", region="eu-west-1")
    roots = (
        snapshot.global_resources
        if resource_type.startswith("aws_iam")
        else snapshot.regions["eu-west-1"].resources
    )
    resource = roots[0]
    assert resource.identity.physical_id is None
    assert resource.identity.logical_address == f"{resource_type}.example"
    assert "/logical/" in resource.id
    assert (resource.identity.region == AwsRegion.GLOBAL) == resource_type.startswith("aws_iam")
    assert load_snapshot(save_snapshot(snapshot, tmp_path / "saved.json")) == snapshot


def test_arn_context_without_explicit_defaults(tmp_path):
    path = write_export(
        tmp_path, [export_resource(arn="arn:aws:ec2:eu-west-1:123456789012:instance/i-one")]
    )
    snapshot = collect(path)
    assert snapshot.account_id == "123456789012"
    assert list(snapshot.regions) == [AwsRegion.EU_WEST_1]


@pytest.mark.parametrize(
    "context,resources,match",
    [
        ({}, [export_resource(id="i-one")], "account_id"),
        ({"account_id": "123456789012"}, [export_resource(id="i-one")], "Supply region"),
        (
            {"account_id": "999999999999", "region": "eu-west-1"},
            [export_resource(owner_id="123456789012")],
            "mixed-account",
        ),
        (
            {"region": "eu-west-1"},
            [
                export_resource(owner_id="123456789012"),
                export_resource("aws_subnet", owner_id="999999999999"),
            ],
            "mixed-account",
        ),
        (
            {"account_id": "123456789012", "region": "eu-west-1"},
            [export_resource(region="us-east-1")],
            "Conflicting",
        ),
        (
            {"account_id": "123456789012"},
            [export_resource(region="future-region-99")],
            "Unsupported",
        ),
    ],
)
def test_reject_ambiguous_or_conflicting_context(tmp_path, context, resources, match):
    with pytest.raises(IngestionError, match=match):
        collect(write_export(tmp_path, resources), **context)


def test_duplicate_physical_identity_is_not_silently_merged(tmp_path):
    first = export_resource(id="i-one")
    second = deepcopy(first)
    second["address"] = "aws_instance.alias"
    with pytest.raises(IngestionError, match="Duplicate physical"):
        collect(
            write_export(tmp_path, [first, second]), account_id="123456789012", region="eu-west-1"
        )


def test_sensitive_identity_is_not_used_for_matching(tmp_path):
    entry = export_resource(id="sensitive-identity-marker", instance_type="sensitive-type-marker")
    entry["sensitive_values"] = {"id": True, "instance_type": True}
    snapshot = collect(
        write_export(tmp_path, [entry]), account_id="123456789012", region="eu-west-1"
    )
    instance = snapshot.regions["eu-west-1"].resources[0]
    assert instance.identity.physical_id is None
    assert instance.instance_type is None
    assert "sensitive-identity-marker" not in snapshot.model_dump_json()
    assert "sensitive-type-marker" not in snapshot.model_dump_json()


def test_missing_parent_stays_root_without_synthesized_resource(tmp_path):
    path = write_export(tmp_path, [export_resource(id="i-one", subnet_id="subnet-missing")])
    snapshot = collect(path, account_id="123456789012", region="eu-west-1")
    roots = snapshot.regions["eu-west-1"].resources
    assert len(list(flatten(roots))) == 1
    assert roots[0].attributes["unresolved_parent"] == "subnet-missing"
    assert snapshot.dependencies_by_region["eu-west-1"][0].target.kind == "unresolved"
    operation = snapshot.coverage.operations[0]
    assert operation.status == "incomplete"
    assert operation.unresolved_values[0].configuration_path.endswith("/values/subnet_id")


def test_empty_export_is_explicit_success_zero(tmp_path):
    snapshot = collect(write_export(tmp_path, []), account_id="123456789012", region="eu-west-1")
    assert not snapshot.regions["eu-west-1"].resources
    assert snapshot.coverage.operations[0].status == "success"
    assert snapshot.coverage.operations[0].result_count == 0


def test_multi_region_owning_arns_and_global_iam(tmp_path):
    path = write_export(
        tmp_path,
        [
            export_resource(id="i-east", arn="arn:aws:ec2:us-east-1:123456789012:instance/i-east"),
            export_resource(
                "aws_subnet",
                id="subnet-west",
                arn="arn:aws:ec2:eu-west-1:123456789012:subnet/subnet-west",
            ),
            export_resource(
                "aws_iam_role",
                id="role-name",
                arn="arn:aws:iam::123456789012:role/role-name",
                assume_role_policy="do-not-store-policy",
            ),
        ],
    )
    snapshot = collect(path)
    assert set(snapshot.regions) == {AwsRegion.US_EAST_1, AwsRegion.EU_WEST_1}
    assert len(snapshot.global_resources) == 1
    role = snapshot.global_resources[0]
    assert role.resource_type == "AWS::IAM::Role"
    assert role.identity.physical_id == "role-name"
    assert role.trust_document is None
    assert snapshot.coverage.account_wide
    assert (
        next(op for op in snapshot.coverage.operations if op.region == "global").status
        == "incomplete"
    )
    assert "do-not-store-policy" not in snapshot.model_dump_json()
    assert load_snapshot(save_snapshot(snapshot, tmp_path / "multi-region.json")) == snapshot


def test_import_is_offline_and_does_not_execute_terraform(monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail(
            "State import must not execute commands, prepare AWS sessions, or resolve hosts"
        )

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    context = AdapterContext(clock=lambda: NOW, session_factory=forbidden)
    request = TerraformStateRequest(path=FIXTURE, account_id="123456789012", region="eu-west-1")
    assert collect_snapshots(request, context=context)[0].source.kind == "terraform_state"


def test_request_and_wrong_adapter_kind_validation(tmp_path):
    with pytest.raises(ValidationError, match="concrete"):
        TerraformStateRequest(path=FIXTURE, region="global")
    with pytest.raises(ValidationError):
        TerraformStateRequest(path=FIXTURE, account_id="bad")
    from archdiscov.ingestion.requests import SourceRequest

    with pytest.raises(IngestionError, match="Expected a TerraformStateRequest"):
        TerraformStateAdapter().collect(SourceRequest(kind="bad"), context=CONTEXT)
