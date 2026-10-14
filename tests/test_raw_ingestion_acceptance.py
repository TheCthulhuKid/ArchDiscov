"""Offline installed-entrypoint journeys for snapshots and raw IaC exports."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from archdiscov.services import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
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


@pytest.fixture(params=["script", "module"])
def offline_cli(request, tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(
            ("AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "OPENAI_", "ANTHROPIC_", "BOTO_")
        )
    }
    env.update(
        AWS_EC2_METADATA_DISABLED="true",
        AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "no-aws-credentials"),
        AWS_CONFIG_FILE=str(tmp_path / "no-aws-config"),
        BOTO_CONFIG=str(tmp_path / "no-boto-config"),
        NO_COLOR="1",
        COLUMNS="160",
    )
    command = (
        [str(Path(sys.executable).with_name("archdiscov"))]
        if request.param == "script"
        else [sys.executable, "-m", "archdiscov"]
    )

    def run(*arguments, exit_code=0, json_output=True):
        result = subprocess.run(
            [*command, *map(str, arguments), *(["--json"] if json_output else [])],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == exit_code, (result.stdout, result.stderr)
        assert "Traceback" not in result.stdout + result.stderr
        for secret in SECRETS:
            assert secret not in result.stdout + result.stderr
        if json_output:
            assert result.stderr == ""
            return json.loads(result.stdout)
        return result

    return run


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def assert_summary(result, snapshot, output):
    assert result["snapshot_id"] == str(snapshot.snapshot_id)
    assert result["path"] == str(output)
    assert type(result["complete"]) is bool
    assert result["source_kind"] == snapshot.source.kind
    assert isinstance(result["operations"], list)
    fields = ("region", "service", "operation", "status", "result_count")
    assert [tuple(op[field] for field in fields) for op in result["operations"]] == [
        tuple(op.model_dump(mode="json")[field] for field in fields)
        for op in snapshot.coverage.operations
    ]


@pytest.mark.parametrize(
    "format_name,fixture_name",
    [
        ("snapshot", "snapshots/hierarchy.json"),
        ("snapshot", "snapshots/partial-coverage.json"),
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
    options = [] if format_name == "snapshot" else ["--format", format_name, *CONTEXT]
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
    assert_summary(imported, snapshot, output)
    assert snapshot.account_id == ACCOUNT
    assert REGION in snapshot.regions
    resources = list(flatten(snapshot.regions[REGION].resources))

    if format_name == "snapshot":
        assert snapshot == load_snapshot(fixture)
        evidence_id = "sg-rule" if "partial" in fixture_name else "instance"
    else:
        assert imported["complete"] is False
        assert any(op.status == "incomplete" for op in snapshot.coverage.operations)
        assert snapshot.source.path == str(source)
        if format_name.startswith("terraform-"):
            assert snapshot.source.workspace == "acceptance"
            instance = next(
                r
                for r in resources
                if r.identity.logical_address == 'module.app.aws_instance.web["blue/green"]'
            )
            evidence_id = instance.fact_evidence["/instance_type"][0]
            if format_name == "terraform-state":
                assert instance.instance_type == "t3.micro"
                assert instance.identity.physical_id == "i-state"
                expected_path = (
                    "/values/root_module/child_modules/0/resources/1/values/instance_type"
                )
            else:
                assert instance.instance_type == "t3.small"
                assert instance.lifecycle.actions == ["update"]
                assert "subnet_id" not in instance.attributes
                expected_path = "/resource_changes/2/change/after/instance_type"
            assert snapshot.evidence[evidence_id].configuration_path == expected_path
            assert snapshot.evidence[evidence_id].value == instance.instance_type
        else:
            assert snapshot.source.stack_name == "acceptance"
            assert snapshot.source.stack_id == STACK_ID
            network = next(
                r for r in resources if r.identity.logical_address == f"{STACK_ID}/Network"
            )
            assert network.name == "qa-network"
            assert [str(cidr) for cidr in network.cidr_blocks] == ["10.20.0.0/16"]
            evidence_id = network.fact_evidence["/cidr_blocks/0"][0]
            assert snapshot.evidence[evidence_id].configuration_path == (
                "/Resources/Network/Properties/CidrBlock"
            )
            assert snapshot.evidence[evidence_id].value == "10.20.0.0/16"

    if "partial" in fixture_name:
        assert imported["complete"] is False
    saved_bytes = output.read_bytes()
    for secret in SECRETS:
        assert secret.encode() not in saved_bytes
    inspected = offline_cli("evidence", evidence_id, "--snapshot", output)
    assert inspected["snapshot_id"] == str(snapshot.snapshot_id)
    assert inspected["snapshot_source"] == snapshot.source.model_dump(mode="json")
    assert inspected["snapshot_path"] == str(output)
    assert inspected["evidence_id"] == evidence_id
    assert inspected["evidence"] == snapshot.evidence[evidence_id].model_dump(mode="json")
    assert inspected["resource"]["id"] == inspected["evidence"]["resource_id"]
    assert inspected["fact_paths"]
    for fact_path in inspected["fact_paths"]:
        assert evidence_id in inspected["resource"]["fact_evidence"][fact_path]

    conflict = offline_cli(*arguments, exit_code=3)
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert output.read_bytes() == saved_bytes
    text_output = tmp_path / "text-saved.json"
    readable = offline_cli("ingest", source, "--output", text_output, *options, json_output=False)
    assert readable.stderr == ""
    assert "Saved snapshot" in readable.stdout
    if not imported["complete"]:
        assert "PARTIAL" in readable.stdout
    assert load_snapshot(text_output).source == snapshot.source
    assert source.read_bytes() == fixture.read_bytes() == source_bytes
    assert output.read_bytes() == saved_bytes


@pytest.mark.parametrize(
    "format_name", ["snapshot", "terraform-state", "terraform-plan", "cloudformation"]
)
def test_malformed_input_is_safe_and_creates_no_output(offline_cli, tmp_path, format_name):
    source = tmp_path / "malformed.json"
    source.write_text('{"parameter-secret-must-not-persist": [')
    output = tmp_path / "not-created.json"
    result = offline_cli(
        "ingest",
        source,
        "--output",
        output,
        "--format",
        format_name,
        *(() if format_name == "snapshot" else CONTEXT),
        exit_code=2,
    )
    assert result["error"]["code"]
    assert result["error"]["message"]
    assert not output.exists()
    assert source.read_text() == '{"parameter-secret-must-not-persist": ['


@pytest.mark.parametrize(
    "format_name,fixture_name,options",
    [
        ("terraform-state", "terraform/state.json", ("--account-id", "wrong", "--region", REGION)),
        ("terraform-plan", "terraform/plan.json", ("--account-id", ACCOUNT)),
        ("cloudformation", "cloudformation/template.yaml", ("--region", REGION)),
        ("cloudformation", "cloudformation/template.yaml", ("--account-id", ACCOUNT)),
    ],
)
def test_missing_or_invalid_context_creates_no_output(
    offline_cli, tmp_path, format_name, fixture_name, options
):
    output = tmp_path / "not-created.json"
    result = offline_cli(
        "ingest",
        FIXTURES / fixture_name,
        "--output",
        output,
        "--format",
        format_name,
        *options,
        exit_code=2,
    )
    assert result["error"]["code"]
    assert not output.exists()


@pytest.mark.parametrize("contents", ['["parameter-secret-must-not-persist"]', '{"x":'])
def test_parameters_file_must_be_a_json_object(offline_cli, tmp_path, contents):
    parameters = tmp_path / "parameters.json"
    parameters.write_text(contents)
    output = tmp_path / "not-created.json"
    result = offline_cli(
        "ingest",
        FIXTURES / "cloudformation/template.yaml",
        "--output",
        output,
        "--format",
        "cloudformation",
        *CONTEXT,
        "--parameters-file",
        parameters,
        exit_code=2,
    )
    assert result["error"]["code"]
    assert not output.exists()
    assert parameters.read_text() == contents


def test_cloudformation_repeatable_sensitive_paths(offline_cli, tmp_path):
    source = tmp_path / "template.json"
    source.write_text(
        json.dumps(
            {
                "Resources": {
                    "Server": {
                        "Type": "AWS::EC2::Instance",
                        "Properties": {
                            "InstanceType": "parameter-secret-must-not-persist",
                            "Tags": [{"Key": "Name", "Value": "fixture-secret-must-not-persist"}],
                        },
                    }
                },
            }
        )
    )
    output = tmp_path / "saved.json"
    result = offline_cli(
        "ingest",
        source,
        "--output",
        output,
        "--format",
        "cloudformation",
        *CONTEXT,
        "--sensitive-path",
        "/Resources/Server/Properties/InstanceType",
        "--sensitive-path",
        "/Resources/Server/Properties/Tags/0/Value",
    )
    snapshot = load_snapshot(output)
    assert_summary(result, snapshot, output)
    (server,) = snapshot.regions[REGION].resources
    assert server.instance_type is None
    assert server.name is None
    assert any(e.value_status == "redacted" for e in snapshot.evidence.values())
    assert all(secret.encode() not in output.read_bytes() for secret in SECRETS)


@pytest.mark.parametrize("format_name", ["terraform-state", "terraform-plan"])
def test_terraform_infers_context_from_arn(offline_cli, tmp_path, format_name):
    values = {
        "id": "i-inferred",
        "arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-inferred",
        "instance_type": "t3.micro",
    }
    resource = {
        "address": "aws_instance.example",
        "mode": "managed",
        "type": "aws_instance",
        "provider_name": "registry.terraform.io/hashicorp/aws",
    }
    if format_name == "terraform-state":
        resource.update(values=values, sensitive_values={})
        document = {"format_version": "1.0", "values": {"root_module": {"resources": [resource]}}}
    else:
        resource["change"] = {"actions": ["no-op"], "before": values, "after": values}
        document = {
            "format_version": "1.2",
            "planned_values": {"root_module": {}},
            "resource_changes": [resource],
        }
    source = tmp_path / "inferred.json"
    source.write_text(json.dumps(document))
    output = tmp_path / "saved.json"
    result = offline_cli("ingest", source, "--output", output, "--format", format_name)
    snapshot = load_snapshot(output)
    assert_summary(result, snapshot, output)
    assert snapshot.account_id == ACCOUNT
    (instance,) = snapshot.regions[REGION].resources
    assert instance.identity.physical_id == "i-inferred"
    assert instance.instance_type == "t3.micro"
    assert all(op.status == "success" for op in snapshot.coverage.operations)
    assert result["complete"] is True
