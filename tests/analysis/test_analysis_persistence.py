import errno
import json

import pytest

from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.services import (
    AnalysisValidationError,
    SnapshotConflictError,
    SnapshotIOError,
    SnapshotJSONError,
    load_analysis,
    load_snapshot,
    publication,
    save_analysis,
)
from tests.paths import FIXTURES_ROOT


def fixture(name):
    return json.loads((FIXTURES_ROOT / "analysis" / f"{name}.json").read_text())


def ownership_snapshot():
    return load_snapshot(FIXTURES_ROOT / "analysis" / "ownership-snapshot.json")


def test_results_round_trip_through_files(tmp_path):
    snapshot = ownership_snapshot()
    path = tmp_path / "result.json"
    assert save_analysis(fixture("ownership"), path, snapshot=snapshot) == path
    assert path.read_text(encoding="utf-8").endswith("}\n")
    saved = load_analysis(path, snapshot=snapshot)
    assert saved == AnalysisResult.model_validate(fixture("ownership"))
    assert load_analysis(path) == saved
    assert sorted(tmp_path.iterdir()) == [path]


def test_existing_destinations_are_never_overwritten(tmp_path):
    path = tmp_path / "result.json"
    path.write_text("precious", encoding="utf-8")
    with pytest.raises(SnapshotConflictError):
        save_analysis(fixture("ownership"), path, snapshot=ownership_snapshot())
    assert path.read_text(encoding="utf-8") == "precious"
    assert sorted(tmp_path.iterdir()) == [path]


def test_unverified_results_are_rejected_before_anything_is_written(tmp_path):
    wrong_snapshot = {**fixture("ownership"), "snapshot_id": "00000000-0000-4000-8000-0000000000ff"}
    path = tmp_path / "result.json"
    with pytest.raises(AnalysisValidationError) as error:
        save_analysis(wrong_snapshot, path, snapshot=ownership_snapshot())
    assert error.value.path == path
    assert list(tmp_path.iterdir()) == []


def test_publication_failures_leave_no_destination_or_temporary_file(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "private-secret")

    monkeypatch.setattr(publication.os, "link", fail)
    with pytest.raises(SnapshotIOError) as error:
        save_analysis(fixture("ownership"), tmp_path / "result.json", snapshot=ownership_snapshot())
    assert "analysis result" in str(error.value)
    assert "private-secret" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_load_rejects_unreadable_and_malformed_files(tmp_path):
    with pytest.raises(SnapshotIOError):
        load_analysis(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{\n  "schema_version": ', encoding="utf-8")
    with pytest.raises(SnapshotJSONError) as error:
        load_analysis(broken)
    assert (error.value.code, error.value.line) == ("invalid_json", 2)


def test_tampered_and_mismatched_files_are_rejected(tmp_path):
    path = tmp_path / "result.json"
    save_analysis(fixture("ownership"), path, snapshot=ownership_snapshot())
    with pytest.raises(AnalysisValidationError) as error:
        load_analysis(path, snapshot=load_snapshot(FIXTURES_ROOT / "snapshots" / "network.json"))
    assert error.value.path == path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["findings"][0]["evidence_ids"] = ["missing-evidence"]
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    # Finding IDs are derived from content, so edits fail even the contract-only check.
    with pytest.raises(AnalysisValidationError) as error:
        load_analysis(tampered)
    assert error.value.path == tampered
