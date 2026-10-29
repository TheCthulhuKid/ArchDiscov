from archdiscov.cli_collection import outcome_data, print_outcomes
from archdiscov.cli_ingestion import ingestion_data
from archdiscov.services import AccountOutcome, IngestResult, load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "partial-coverage.json"


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
