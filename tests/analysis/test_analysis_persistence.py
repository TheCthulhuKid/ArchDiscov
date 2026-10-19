import errno
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.services import (
    AnalysisValidationError,
    SnapshotConflictError,
    SnapshotIOError,
    SnapshotJSONError,
    analyses,
    load_analysis,
    load_snapshot,
    publication,
    save_analysis,
    upgrade_snapshot,
)
from tests.paths import FIXTURES_ROOT

CASES = [
    ("ownership", "analysis/ownership-snapshot.json"),
    ("partial-path", "snapshots/dependency-targets.json"),
]


def fixture(name):
    return json.loads((FIXTURES_ROOT / "analysis" / f"{name}.json").read_text())


def snapshot_for(snapshot_path, modern=False):
    snapshot = load_snapshot(FIXTURES_ROOT / snapshot_path)
    return upgrade_snapshot(snapshot) if modern else snapshot


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize(("name", "snapshot_path"), CASES)
def test_results_round_trip_through_files(tmp_path, name, snapshot_path, modern):
    snapshot = snapshot_for(snapshot_path, modern)
    path = tmp_path / "result.json"
    assert save_analysis(fixture(name), path, snapshot=snapshot) == path
    assert path.read_text(encoding="utf-8").endswith("}\n")
    saved = load_analysis(path, snapshot=snapshot)
    assert saved == AnalysisResult.model_validate(fixture(name))
    assert load_analysis(path) == saved
    assert sorted(tmp_path.iterdir()) == [path]


def test_existing_destinations_are_never_overwritten(tmp_path):
    snapshot = snapshot_for(CASES[0][1])
    path = tmp_path / "result.json"
    path.write_text("precious", encoding="utf-8")
    with pytest.raises(SnapshotConflictError):
        save_analysis(fixture("ownership"), path, snapshot=snapshot)
    assert path.read_text(encoding="utf-8") == "precious"
    assert sorted(tmp_path.iterdir()) == [path]


def test_concurrent_saves_publish_exactly_once(tmp_path):
    snapshot = snapshot_for(CASES[0][1])
    path = tmp_path / "result.json"

    def save():
        try:
            return save_analysis(fixture("ownership"), path, snapshot=snapshot)
        except SnapshotConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(), range(2)))
    assert sum(result == path for result in results) == 1
    assert sum(isinstance(result, SnapshotConflictError) for result in results) == 1
    assert sorted(tmp_path.iterdir()) == [path]


def test_unverified_results_are_rejected_before_anything_is_written(tmp_path):
    snapshot = snapshot_for(CASES[0][1])
    wrong_snapshot = {**fixture("ownership"), "snapshot_id": "00000000-0000-4000-8000-0000000000ff"}
    dangling = deepcopy(fixture("ownership"))
    dangling["findings"][0]["evidence_ids"] = ["missing-evidence"]
    for index, data in enumerate([wrong_snapshot, dangling, {}]):
        path = tmp_path / f"{index}.json"
        with pytest.raises(AnalysisValidationError) as error:
            save_analysis(data, path, snapshot=snapshot)
        assert error.value.path == path
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["link", "write"])
def test_publication_failures_leave_no_destination_or_temporary_file(
    tmp_path, monkeypatch, failure
):
    snapshot = snapshot_for(CASES[0][1])
    path = tmp_path / "result.json"

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "private-secret")

    monkeypatch.setattr(publication.os, "link" if failure == "link" else "fsync", fail)
    with pytest.raises(SnapshotIOError) as error:
        save_analysis(fixture("ownership"), path, snapshot=snapshot)
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
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(SnapshotJSONError) as error:
        load_analysis(binary)
    assert error.value.code == "invalid_encoding"
    nonstandard = tmp_path / "nan.json"
    nonstandard.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(SnapshotJSONError):
        load_analysis(nonstandard)


def test_tampered_and_mismatched_files_are_rejected_with_safe_diagnostics(tmp_path):
    snapshot = snapshot_for(CASES[0][1])
    other = snapshot_for("snapshots/network.json")
    path = tmp_path / "result.json"
    save_analysis(fixture("ownership"), path, snapshot=snapshot)

    with pytest.raises(AnalysisValidationError) as error:
        load_analysis(path, snapshot=other)
    assert error.value.path == path

    data = json.loads(path.read_text(encoding="utf-8"))
    data["findings"][0]["evidence_ids"] = ["missing-evidence"]
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    # Finding IDs are derived from content, so edits fail even the contract-only check.
    for selected in (None, snapshot):
        with pytest.raises(AnalysisValidationError) as error:
            load_analysis(tampered, snapshot=selected)
        assert error.value.path == tampered

    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({**data, "SECRET-FIELD": "SECRET-VALUE"}), encoding="utf-8")
    with pytest.raises(AnalysisValidationError) as error:
        load_analysis(secret)
    rendered = f"{error.value} {error.value.details}"
    assert "SECRET" not in rendered


def test_unexpected_defects_are_not_reported_as_serialization_failures(tmp_path, monkeypatch):
    def defect(*args, **kwargs):
        raise ValueError("internal defect")

    monkeypatch.setattr(analyses, "validate_analysis_result", defect)
    with pytest.raises(ValueError, match="internal defect"):
        save_analysis(fixture("ownership"), tmp_path / "r.json", snapshot=snapshot_for(CASES[0][1]))
    assert list(tmp_path.iterdir()) == []
