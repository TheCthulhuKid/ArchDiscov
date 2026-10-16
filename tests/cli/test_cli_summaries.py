import json

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.cli_collection import outcome_data, print_outcomes
from archdiscov.cli_ingestion import ingestion_data
from archdiscov.services import AccountOutcome, IngestResult, load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "partial-coverage.json"


@pytest.mark.parametrize("json_output", [False, True])
def test_partial_import_reports_operation_failures(tmp_path, json_output):
    flags = ["--json"] if json_output else []
    result = CliRunner().invoke(
        app, ["ingest", str(FIXTURE), "-o", str(tmp_path / "saved.json"), *flags]
    )
    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["complete"] is False
        failed = next(op for op in payload["operations"] if op["status"] == "failed")
        assert failed["operation"] == "DescribeRouteTables"
        assert failed["failures"] == ["UnauthorizedOperation"]
    else:
        assert "PARTIAL" in result.stdout
        assert "eu-west-1 ec2 DescribeRouteTables: failed [UnauthorizedOperation]" in result.stdout
        assert "DescribeInstances: success (0 results)" in result.stdout


def test_import_and_collection_share_operation_diagnostics(tmp_path, capsys):
    snapshot = load_snapshot(FIXTURE)
    path = tmp_path / "snapshot.json"
    imported = ingestion_data(IngestResult(snapshot=snapshot, path=path))
    collected = outcome_data(
        AccountOutcome(account_id=snapshot.account_id, snapshot=snapshot, path=path)
    )
    assert imported["operations"] == collected["operations"]
    print_outcomes([collected])
    assert "DescribeRouteTables: failed [UnauthorizedOperation]" in capsys.readouterr().out
