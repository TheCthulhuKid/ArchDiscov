import json
from ipaddress import ip_network

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.parameters import load_parameters
from archdiscov.services import IngestFormat, importing, ingest_snapshot, load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT
runner = CliRunner()


@pytest.mark.parametrize("explicit", [False, True])
def test_default_and_explicit_snapshot_import(tmp_path, explicit):
    fixture = FIXTURES / "snapshots" / "partial-coverage.json"
    output = tmp_path / "saved.json"
    result = ingest_snapshot(
        fixture, output, **({"source_format": IngestFormat.SNAPSHOT} if explicit else {})
    )
    assert result.snapshot == load_snapshot(fixture) == load_snapshot(output)
    assert result.path == output
    assert result.complete is False


@pytest.mark.parametrize(
    ("source_format", "options"),
    [
        ("unknown-secret", {}),
        ("snapshot", {"account_id": "123456789012"}),
        ("snapshot", {"region": "eu-west-1"}),
        ("snapshot", {"workspace": "dev"}),
        ("snapshot", {"stack_name": "Example"}),
        ("snapshot", {"stack_id": "private-value"}),
        ("snapshot", {"parameters_file": "missing.json"}),
        ("snapshot", {"sensitive_paths": ("/Properties/Password",)}),
        ("terraform-state", {"stack_name": "Example"}),
        ("terraform-plan", {"parameters_file": "missing.json"}),
        ("terraform-plan", {"sensitive_paths": ("/Password",)}),
        ("terraform-state", {"region": "private-value"}),
        ("terraform-plan", {"account_id": "private-value"}),
        ("terraform-state", {"region": "global"}),
        ("cloudformation", {}),
        ("cloudformation", {"account_id": "123456789012"}),
        ("cloudformation", {"region": "eu-west-1"}),
        ("cloudformation", {"workspace": "dev"}),
        (
            "cloudformation",
            {"account_id": "private-value", "region": "eu-west-1"},
        ),
    ],
)
def test_invalid_selection_or_context_never_reads_or_publishes(tmp_path, source_format, options):
    output = tmp_path / "saved.json"
    with pytest.raises(IngestionError) as caught:
        ingest_snapshot(tmp_path / "not-read", output, source_format=source_format, **options)
    assert caught.value.code == "invalid_request"
    assert "private-value" not in str(caught.value)
    assert "unknown-secret" not in str(caught.value)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    [
        b'{"Secret":"private-value",',
        b'{"Secret":"private-value","Secret":"another"}',
        b'["private-value"]',
        b'{"Secret":NaN}',
        b'{"Secret":Infinity}',
        b'{"Secret":-Infinity}',
        b'{"Secret":"\xff"}',
        b'{" ":"private-value"}',
        b"[" * 1000 + b"]" * 1000,
        b'{"Secret":"' + b"x" * (1024 * 1024) + b'"}',
    ],
)
def test_parameters_file_rejects_unsafe_shapes_without_values(tmp_path, content):
    path = tmp_path / "parameters.json"
    path.write_bytes(content)
    with pytest.raises(IngestionError, match="bounded UTF-8 JSON object") as caught:
        load_parameters(path)
    assert caught.value.code == "invalid_request"
    assert "private-value" not in str(caught.value)
    assert path.read_bytes() == content


@pytest.mark.parametrize("directory", [False, True])
def test_unreadable_parameters_file_is_safe(tmp_path, directory):
    path = tmp_path if directory else tmp_path / "missing.json"
    with pytest.raises(IngestionError, match="Parameters file"):
        load_parameters(path)


def test_parameters_file_preserves_json_values(tmp_path):
    path = tmp_path / "parameters.json"
    values = {"Name": "example", "Count": 0, "Ports": [80, 443], "Enabled": False}
    path.write_text(json.dumps(values))
    assert load_parameters(path) == values


def test_cloudformation_parameters_and_sensitive_paths_are_redacted(tmp_path):
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "Parameters": {
                    "NetworkCidr": {"Type": "String"},
                    "Protected": {"Type": "String", "NoEcho": True},
                },
                "Resources": {
                    "Network": {
                        "Type": "AWS::EC2::VPC",
                        "Properties": {
                            "CidrBlock": {"Ref": "NetworkCidr"},
                            "Tags": [
                                {"Key": "SecretNote", "Value": {"Ref": "Protected"}},
                                {"Key": "Name", "Value": "source-private-value"},
                            ],
                        },
                    }
                },
            }
        )
    )
    parameters = tmp_path / "parameters.json"
    parameters.write_text(
        json.dumps({"NetworkCidr": "10.20.0.0/16", "Protected": "parameter-private-value"})
    )
    output = tmp_path / "saved.json"
    result = ingest_snapshot(
        template,
        output,
        source_format="cloudformation",
        account_id="123456789012",
        region="eu-west-1",
        stack_name="Example",
        parameters_file=parameters,
        sensitive_paths=("/Resources/Network/Properties/Tags/1/Value",),
    )
    assert result.snapshot.regions["eu-west-1"].resources[0].cidr_blocks == [
        ip_network("10.20.0.0/16")
    ]
    assert result.snapshot.source.stack_name == "Example"
    assert "parameter-private-value" not in output.read_text()
    assert "source-private-value" not in output.read_text()
    assert load_snapshot(output) == result.snapshot


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_context_diagnostics_never_echo_values(tmp_path, json_output):
    output = tmp_path / "saved.json"
    result = runner.invoke(
        app,
        [
            "ingest",
            "not-read",
            "-o",
            str(output),
            "--format",
            "terraform-state",
            "--account-id",
            "private-value",
            *(["--json"] if json_output else []),
        ],
    )
    assert result.exit_code == 2
    assert "private-value" not in result.output
    assert "invalid_request" in result.output
    assert not output.exists()


def test_invalid_format_is_usage_error_without_guessing(tmp_path):
    output = tmp_path / "saved.json"
    result = runner.invoke(
        app, ["ingest", "template.yaml", "-o", str(output), "--format", "yaml", "--json"]
    )
    assert result.exit_code == 2
    assert not output.exists()


def test_empty_terraform_import_reports_successful_zero(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"format_version": "1.0", "values": {"root_module": {}}}))
    result = ingest_snapshot(
        source,
        tmp_path / "snapshot.json",
        source_format="terraform-state",
        account_id="123456789012",
        region="eu-west-1",
        workspace="development",
    )
    assert result.complete is True
    assert result.snapshot.source.workspace == "development"
    assert result.snapshot.coverage.operations[0].result_count == 0
    assert result.snapshot.coverage.operations[0].status == "success"


def test_wrong_adapter_cardinality_publishes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(importing, "collect_snapshots", lambda *args, **kwargs: [])
    with pytest.raises(IngestionError, match="one snapshot"):
        ingest_snapshot(
            tmp_path / "source.json", tmp_path / "snapshot.json", source_format="terraform-plan"
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("json_output", [False, True])
def test_parameter_parse_error_publishes_nothing_and_is_safe(tmp_path, json_output):
    parameters = tmp_path / "parameters.json"
    parameters.write_text('{"Secret":"private-value",')
    output = tmp_path / "snapshot.json"
    result = runner.invoke(
        app,
        [
            "ingest",
            "not-read",
            "-o",
            str(output),
            "--format",
            "cloudformation",
            "--account-id",
            "123456789012",
            "--region",
            "eu-west-1",
            "--parameters-file",
            str(parameters),
            *(["--json"] if json_output else []),
        ],
    )
    assert result.exit_code == 2
    assert "private-value" not in result.output
    assert "invalid_request" in result.output
    assert not output.exists()
