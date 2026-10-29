"""The installed Azure workflow outside the checkout and without credentials.

The Azure snapshot contract is `tests/models/test_azure_snapshot.py`'s, the commands' readable
and JSON forms are `tests/cli/test_cli.py`'s, and each AWS-only analyzer's unsupported result on
Azure is its analyzer tests' and `test_phase_three_audit.py`'s.
"""

from pathlib import Path

from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import Workspace

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_installed_azure_workflow_outside_checkout_without_credentials(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    output = ws.path("snapshots", "azure.json")
    summary = ws.json("ingest", FIXTURES / "azure.json", "-o", output)
    assert summary["source_kind"] == "azure_fixture"
    assert summary["operations"][0]["platform"] == "azure"
    stored = output.read_bytes()
    payload = ws.json("evidence", "vm-size", "--snapshot", output)
    assert payload["fact_paths"] == ["/vm_size"]
    assert payload["evidence"]["scope"]["platform"] == "azure"
    assert payload["evidence"]["location"] == "eastus"
    assert output.read_bytes() == stored
    assert ws.guard.attempts == []
