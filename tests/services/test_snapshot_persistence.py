import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.services import (
    SnapshotConflictError,
    SnapshotIOError,
    SnapshotJSONError,
    SnapshotValidationError,
    load_snapshot,
    publication,
    save_snapshot,
    validate_snapshot,
)
from archdiscov.services.publication import read_json
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.fixture
def snapshot() -> ArchitectureSnapshot:
    return ArchitectureSnapshot.model_validate_json((FIXTURES / "hierarchy.json").read_text())


def instance(snapshot: ArchitectureSnapshot):
    return snapshot.regions["eu-west-1"].resources[0].children[0].children[0]


def test_nested_mutations_are_rechecked_and_never_saved(
    snapshot: ArchitectureSnapshot, tmp_path: Path
) -> None:
    mapping = snapshot.model_dump(mode="json")
    mapping["regions"] = snapshot.regions
    instance(snapshot).id = "invalid"
    for source in (snapshot, mapping):
        with pytest.raises(SnapshotValidationError) as error:
            validate_snapshot(source)
        assert error.value.code == "invalid_snapshot"
        assert error.value.__cause__ is not None
    path = tmp_path / "snapshot.json"
    with pytest.raises(SnapshotValidationError) as error:
        save_snapshot(snapshot, path)
    assert error.value.path == path
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b'{\n"private-secret": }', "invalid_json"),
        (b"\xff", "invalid_encoding"),
        (b'{"schema_version": "2", "schema_version": "private-secret"}', "invalid_json"),
        (b"NaN", "invalid_json"),
    ],
    ids=["syntax", "encoding", "duplicate-key", "non-finite"],
)
def test_load_format_errors(tmp_path: Path, content: bytes, code: str) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes(content)
    with pytest.raises(SnapshotJSONError) as error:
        load_snapshot(path)
    assert (error.value.code, error.value.path) == (code, path)
    assert error.value.__cause__ is not None
    assert "private-secret" not in str(error.value)
    if content.startswith(b"{\n"):
        assert (error.value.line, error.value.column) == (2, 19)


def test_an_oversized_json_file_is_refused_unread(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("[" + "0," * 600 + "0]", encoding="utf-8")
    with pytest.raises(SnapshotJSONError) as error:
        read_json(path, subject="snapshot", limit=1_000)
    assert (error.value.code, error.value.path) == ("json_too_large", path)
    assert str(error.value) == "Snapshot JSON exceeds 1000 bytes."
    assert read_json(path, subject="snapshot", limit=path.stat().st_size)[0] == 0


def test_load_filesystem_error_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes((FIXTURES / "hierarchy.json").read_bytes())

    def fail_open(*args, **kwargs):
        raise PermissionError("private-secret")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(SnapshotIOError) as error:
        load_snapshot(path)
    assert error.value.code == "snapshot_io_error"
    assert error.value.path == path
    assert isinstance(error.value.__cause__, OSError)
    assert "private-secret" not in str(error.value)


def secret_version(data: dict) -> None:
    data["schema_version"] = "private-secret"


def unknown_target(data: dict) -> None:
    data["dependencies_by_region"]["eu-west-1"][0]["target_id"] = "private-secret"


def unknown_resource_type(data: dict) -> None:
    data["regions"]["eu-west-1"]["resources"][0]["resource_type"] = "private-secret"


@pytest.mark.parametrize(
    ("defect", "code", "location", "issue_type"),
    [
        (secret_version, "unsupported_schema_version", ("schema_version",), None),
        (unknown_target, "invalid_snapshot", (), "value_error"),
        (
            unknown_resource_type,
            "invalid_snapshot",
            ("regions", "eu-west-1", "resources", 0),
            "union_tag_invalid",
        ),
    ],
)
def test_load_contract_errors_have_safe_diagnostics(
    tmp_path: Path, defect, code: str, location: tuple, issue_type: str | None
) -> None:
    data = json.loads((FIXTURES / "hierarchy.json").read_text())
    defect(data)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SnapshotValidationError) as error:
        load_snapshot(path)
    issue = error.value.details[0]
    assert (error.value.path, error.value.code) == (path, code)
    assert (issue.location, issue.type) == (location, issue_type or code)
    assert "private-secret" not in repr(error.value.details)
    assert "private-secret" not in str(error.value)
    assert not hasattr(issue, "input")
    assert not hasattr(issue, "ctx")


@pytest.mark.parametrize(
    "fixture", [FIXTURES / "hierarchy.json", FIXTURES / "azure.json"], ids=lambda path: path.name
)
def test_persistence_round_trip(fixture: Path, tmp_path: Path) -> None:
    snapshot = load_snapshot(fixture)
    path = tmp_path / fixture.name
    assert save_snapshot(snapshot, str(path)) == path
    assert load_snapshot(path) == snapshot
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert f'\n  "schema_version": "{json.loads(content)["schema_version"]}"' in content
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(tmp_path.iterdir()) == [path]


def test_validation_and_save_detach_and_sanitize_the_callers_snapshot(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
) -> None:
    resource = instance(snapshot)
    resource.attributes["password"] = "private-password"
    snapshot.evidence["instance"].value = {"token": "private-token"}
    mapping = snapshot.model_dump(mode="json")
    mapping["regions"] = snapshot.regions
    for validated in (validate_snapshot(snapshot), validate_snapshot(mapping)):
        assert validated is not snapshot
        assert instance(validated) is not resource
        assert instance(validated).attributes["password"] == "[REDACTED]"
        assert validated.evidence["instance"].value == {"token": "[REDACTED]"}
    path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    assert "private-password" not in path.read_text()
    assert "private-token" not in path.read_text()
    loaded = load_snapshot(path)
    assert instance(loaded).attributes["password"] == "[REDACTED]"
    assert loaded.evidence["instance"].redacted_paths == ["/token"]
    assert loaded.evidence["instance"].value_status == "redacted"
    assert instance(snapshot).attributes["password"] == "private-password"
    assert snapshot.evidence["instance"].value == {"token": "private-token"}


# The link-based publish refuses any existing directory entry; a dangling symlink is the
# entry an existence check would miss.
@pytest.mark.parametrize("existing", ["file", "dangling-symlink"])
def test_save_refuses_every_existing_destination(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    existing: str,
) -> None:
    path = tmp_path / "snapshot.json"
    target = tmp_path / "target.json"
    if existing == "file":
        save_snapshot(snapshot, path)
    else:
        path.symlink_to(target)
    original = path.read_bytes() if path.is_file() else None
    with pytest.raises(SnapshotConflictError) as error:
        save_snapshot(snapshot, path)
    assert error.value.code == "snapshot_conflict"
    assert error.value.path == path
    if original is not None:
        assert path.read_bytes() == original
    else:
        assert path.is_symlink()
        assert path.readlink() == target
    assert not list(tmp_path.glob(".archdiscov-*.tmp"))


def test_two_writers_publish_exactly_one_complete_snapshot(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    link = os.link
    path = tmp_path / "snapshot.json"

    def synchronized_link(source, destination):
        barrier.wait(timeout=5)
        link(source, destination)

    monkeypatch.setattr(publication.os, "link", synchronized_link)

    def write():
        try:
            return save_snapshot(snapshot, path)
        except SnapshotConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sum(result == path for result in results) == 1
    assert sum(isinstance(result, SnapshotConflictError) for result in results) == 1
    assert load_snapshot(path) == snapshot
    assert sorted(tmp_path.iterdir()) == [path]


def test_missing_parent_is_not_created(snapshot: ArchitectureSnapshot, tmp_path: Path) -> None:
    path = tmp_path / "missing" / "snapshot.json"
    with pytest.raises(SnapshotIOError) as error:
        save_snapshot(snapshot, path)
    assert error.value.path == path
    assert not path.parent.exists()


def test_a_failed_write_leaves_no_output(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "snapshot.json"
    original = publication.tempfile.NamedTemporaryFile

    def temporary(*args, **kwargs):
        handle = original(*args, **kwargs)
        write = handle.write

        def partial_write(text):
            write(text[:20])
            raise OSError(errno.EIO, "private-secret")

        handle.write = partial_write
        return handle

    monkeypatch.setattr(publication.tempfile, "NamedTemporaryFile", temporary)
    with pytest.raises(SnapshotIOError) as error:
        save_snapshot(snapshot, path)
    assert error.value.__cause__ is not None
    assert "private-secret" not in str(error.value)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_unserializable_json_values_do_not_create_files(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
) -> None:
    snapshot.metadata["ordinary"] = float("nan")
    with pytest.raises(SnapshotValidationError) as error:
        save_snapshot(snapshot, tmp_path / "snapshot.json")
    assert error.value.details[0].type == "serialization_error"
    assert list(tmp_path.iterdir()) == []


def test_complete_synced_file_exists_before_publication(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "snapshot.json"
    events = []
    fsync = os.fsync
    link = os.link

    def synced(fd):
        events.append("fsync")
        fsync(fd)

    def publish(source, destination):
        assert events == ["fsync"]
        assert not path.exists()
        assert Path(source).parent == path.parent
        assert load_snapshot(source) == snapshot
        assert Path(source).stat().st_mode & 0o777 == 0o600
        events.append("link")
        link(source, destination)

    monkeypatch.setattr(publication.os, "fsync", synced)
    monkeypatch.setattr(publication.os, "link", publish)
    save_snapshot(snapshot, path)
    assert events == ["fsync", "link"]
