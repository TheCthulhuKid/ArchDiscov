"""Offline installed-CLI journey, also runnable with Docker networking disabled."""

import json
from pathlib import Path

from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.installed_cli import Workspace

FIXTURE = FIXTURES_ROOT / "snapshots" / "partial-coverage.json"
EVIDENCE_ID = "sg-rule"


def test_offline_acceptance(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)

    def run(*arguments: str, exit_code: int = 0) -> dict:
        result = ws.run(*arguments, "--json", expect=exit_code)
        assert result.stderr == ""
        return json.loads(result.stdout)

    source = tmp_path / "source.json"
    source.write_bytes(FIXTURE.read_bytes())
    source_bytes = source.read_bytes()
    output = tmp_path / "snapshot.json"
    imported = run("ingest", str(source), "--output", str(output))
    saved = load_snapshot(output)
    assert imported["snapshot_id"] == str(saved.snapshot_id)
    assert imported["path"] == str(output)
    assert imported["complete"] is False
    assert saved == load_snapshot(FIXTURE)
    saved_bytes = output.read_bytes()

    inspected = run("evidence", EVIDENCE_ID, "--snapshot", str(output))
    assert inspected["snapshot_id"] == str(saved.snapshot_id)
    assert inspected["evidence"] == saved.evidence[EVIDENCE_ID].model_dump(mode="json")
    assert inspected["fact_paths"] == ["/attributes/ingress/0"]
    assert inspected["coverage"] == [
        operation.model_dump(mode="json") for operation in saved.coverage.operations
    ]

    conflict = run("ingest", str(source), "--output", str(output), exit_code=3)
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert output.read_bytes() == saved_bytes

    invalid = json.loads(source_bytes)
    invalid["evidence"][EVIDENCE_ID]["resource_id"] = "never-disclose-unknown-owner"
    invalid_source = tmp_path / "invalid-reference.json"
    invalid_source.write_text(json.dumps(invalid))
    invalid_output = tmp_path / "invalid-output.json"
    rejected = run("ingest", str(invalid_source), "--output", str(invalid_output), exit_code=2)
    assert rejected["error"]["code"] == "invalid_snapshot"
    assert rejected["error"]["path"] == str(invalid_source)
    assert "never-disclose-unknown-owner" not in json.dumps(rejected)
    assert not invalid_output.exists()
    assert output.read_bytes() == saved_bytes
    assert source.read_bytes() == source_bytes
    assert ws.guard.attempts == []
