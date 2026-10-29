import json
from ipaddress import ip_network

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.parameters import load_parameters
from archdiscov.services import ingest_snapshot, load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT
runner = CliRunner()


def test_snapshot_import_is_the_default(tmp_path):
    fixture = FIXTURES / "snapshots" / "partial-coverage.json"
    output = tmp_path / "saved.json"
    result = ingest_snapshot(fixture, output)
    assert result.snapshot == load_snapshot(fixture) == load_snapshot(output)
    assert result.path == output
    assert result.complete is False


@pytest.mark.parametrize(
    ("source_format", "options"),
    [
        ("unknown-secret", {}),
        ("snapshot", {"stack_id": "private-value"}),
    ],
    ids=["unknown-format", "snapshot-context"],
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


def test_parameters_file_rejects_malformed_content_without_values(tmp_path):
    content = b'{"Secret":"private-value",'
    path = tmp_path / "parameters.json"
    path.write_bytes(content)
    with pytest.raises(IngestionError, match="bounded UTF-8 JSON object") as caught:
        load_parameters(path)
    assert caught.value.code == "invalid_request"
    assert "private-value" not in str(caught.value)
    assert path.read_bytes() == content


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


def test_cli_context_diagnostics_never_echo_values(tmp_path):
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
        ],
    )
    assert result.exit_code == 2
    assert "private-value" not in result.output
    assert "invalid_request" in result.output
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
