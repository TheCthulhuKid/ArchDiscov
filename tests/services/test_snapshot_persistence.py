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


def test_validation_detaches_and_sanitizes_mutations(snapshot: ArchitectureSnapshot) -> None:
    resource = instance(snapshot)
    resource.attributes["password"] = "private-password"
    snapshot.evidence["instance"].value = {"token": "private-token"}
    validated = validate_snapshot(snapshot)
    assert validated is not snapshot
    assert instance(validated) is not resource
    assert instance(validated).attributes["password"] == "[REDACTED]"
    assert validated.evidence["instance"].value == {"token": "[REDACTED]"}
    assert resource.attributes["password"] == "private-password"
    assert snapshot.evidence["instance"].value == {"token": "private-token"}


@pytest.mark.parametrize("mutation", ["id", "reference", "type"])
def test_validation_rechecks_nested_mutations(
    snapshot: ArchitectureSnapshot, mutation: str
) -> None:
    resource = instance(snapshot)
    if mutation == "id":
        resource.id = "invalid"
    elif mutation == "reference":
        resource.evidence_ids.append("missing")
    else:
        resource.instance_type = object()
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(snapshot)
    assert error.value.code == "invalid_snapshot"
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("version", [2, "4", "private-secret", None])
def test_unsupported_versions_have_safe_diagnostics(version: object) -> None:
    data = json.loads((FIXTURES / "hierarchy.json").read_text())
    data["schema_version"] = version
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(data)
    assert error.value.code == "unsupported_schema_version"
    assert error.value.details[0].location == ("schema_version",)
    assert error.value.details[0].type == "unsupported_schema_version"
    assert "private-secret" not in repr(error.value.details)
    assert "private-secret" not in str(error.value)


def test_validation_diagnostics_exclude_raw_inputs_and_contexts() -> None:
    data = json.loads((FIXTURES / "hierarchy.json").read_text())
    data["regions"]["eu-west-1"]["resources"][0]["resource_type"] = "private-secret"
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(data)
    issue = error.value.details[0]
    assert issue.type == "union_tag_invalid"
    assert issue.location == ("regions", "eu-west-1", "resources", 0)
    assert "private-secret" not in repr(issue)
    assert not hasattr(issue, "input")
    assert not hasattr(issue, "ctx")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.name)
def test_load_committed_fixtures(path: Path) -> None:
    loaded = load_snapshot(str(path))
    if json.loads(path.read_text())["schema_version"] == "3":
        from archdiscov.models.cloud.snapshot import CloudSnapshot

        assert loaded == CloudSnapshot.model_validate_json(path.read_text())
    else:
        assert loaded == ArchitectureSnapshot.model_validate_json(path.read_text())


@pytest.mark.parametrize("content", [b'{\n"private-secret": }', b"\xff", b"NaN"])
def test_load_format_errors(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes(content)
    with pytest.raises(SnapshotJSONError) as error:
        load_snapshot(path)
    assert error.value.path == path
    assert error.value.__cause__ is not None
    assert "private-secret" not in str(error.value)
    if content == b"\xff":
        assert error.value.code == "invalid_encoding"
        assert "UTF-8" in str(error.value)
    else:
        assert error.value.code == "invalid_json"
    if content.startswith(b"{"):
        assert error.value.line == 2
        assert error.value.column == 19


def test_a_snapshot_with_duplicate_keys_is_refused(tmp_path: Path) -> None:
    """A duplicate key would make the file mean something other than it appears to, so it
    is refused as every other reader refuses it, without naming the key or its values."""
    source = (FIXTURES_ROOT / "evaluation" / "readme" / "ownership" / "snapshot.json").read_text()
    path = tmp_path / "snapshot.json"
    duplicate = '"snapshot_id": "00000000-0000-4000-8000-0000000000ff",'
    path.write_text(source.replace("{", "{" + duplicate, 1), encoding="utf-8")
    with pytest.raises(SnapshotJSONError) as error:
        load_snapshot(path)
    assert (error.value.code, error.value.path) == ("invalid_json", path)
    assert str(error.value) == "JSON contains duplicate object keys."
    assert "0000000000ff" not in str(error.value)


def test_an_oversized_json_file_is_refused_unread(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("[" + "0," * 600 + "0]", encoding="utf-8")
    with pytest.raises(SnapshotJSONError) as error:
        read_json(path, subject="snapshot", limit=1_000)
    assert (error.value.code, error.value.path) == ("json_too_large", path)
    assert str(error.value) == "Snapshot JSON exceeds 1000 bytes."
    assert read_json(path, subject="snapshot", limit=path.stat().st_size)[0] == 0


@pytest.mark.parametrize("kind", ["missing", "directory", "read_failure"])
def test_load_filesystem_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    path = tmp_path / "snapshot.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "read_failure":

        def fail_read(*args, **kwargs):
            raise PermissionError("private-secret")

        monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(SnapshotIOError) as error:
        load_snapshot(path)
    assert error.value.code == "snapshot_io_error"
    assert error.value.path == path
    assert isinstance(error.value.__cause__, OSError)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("defect", ["missing_version", "unsupported_version", "reference", "tag"])
def test_load_contract_errors(tmp_path: Path, defect: str) -> None:
    data = json.loads((FIXTURES / "hierarchy.json").read_text())
    if defect == "missing_version":
        del data["schema_version"]
    elif defect == "unsupported_version":
        data["schema_version"] = "4"
    elif defect == "reference":
        data["dependencies_by_region"]["eu-west-1"][0]["target_id"] = "private-secret"
    else:
        data["regions"]["eu-west-1"]["resources"][0]["resource_type"] = "private-secret"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SnapshotValidationError) as error:
        load_snapshot(path)
    assert error.value.path == path
    assert error.value.code == (
        "unsupported_schema_version" if defect == "unsupported_version" else "invalid_snapshot"
    )
    assert "private-secret" not in repr(error.value.details)
    if defect == "missing_version":
        assert error.value.details[0].location == ("schema_version",)
        assert error.value.details[0].type == "missing"
    elif defect == "reference":
        assert "unknown target_id" in error.value.details[0].message


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.name)
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
    if fixture.name == "hierarchy.json":
        loaded = load_snapshot(path)
        vpc = loaded.regions["eu-west-1"].resources[0]
        subnet = vpc.children[0]
        child = instance(loaded)
        assert [str(cidr) for cidr in vpc.cidr_blocks] == ["10.0.0.0/16"]
        assert [str(cidr) for cidr in subnet.cidr_blocks] == ["10.0.1.0/24"]
        assert subnet.availability_zone == "eu-west-1a"
        assert child.instance_type == "t3.micro"
        assert child.name is None
        assert child.id == "123456789012/eu-west-1/ec2/physical/i-web"
        assert child.fact_evidence == {"/instance_type": ["instance"]}
        assert loaded.dependencies_by_region["eu-west-1"][0].evidence_ids == ["instance"]
    if fixture.name == "partial-coverage.json":
        failure = load_snapshot(path).coverage.operations[1].failures[0]
        assert failure.code == "UnauthorizedOperation"
        assert failure.message == "Route-table collection denied"


def test_save_revalidates_and_does_not_mutate_caller(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
) -> None:
    instance(snapshot).attributes["password"] = "private-password"
    snapshot.evidence["instance"].value = {"token": "private-token"}
    path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    assert "private-password" not in path.read_text()
    assert "private-token" not in path.read_text()
    loaded = load_snapshot(path)
    assert instance(loaded).attributes["password"] == "[REDACTED]"
    assert loaded.evidence["instance"].redacted_paths == ["/token"]
    assert loaded.evidence["instance"].value_status == "redacted"
    assert instance(snapshot).attributes["password"] == "private-password"
    assert snapshot.evidence["instance"].value == {"token": "private-token"}
    stored = path.read_bytes()
    instance(loaded).name = "changed"
    instance(loaded).attributes["password"] = "new-secret"
    assert path.read_bytes() == stored
    second = save_snapshot(loaded, tmp_path / "second.json")
    assert load_snapshot(second).snapshot_id == snapshot.snapshot_id


def test_coverage_and_evidence_limitations_survive(tmp_path: Path) -> None:
    data = json.loads((FIXTURES / "partial-coverage.json").read_text())
    operation = data["coverage"]["operations"][2]
    operation.update(
        status="incomplete",
        result_count=1,
        unresolved_values=[
            {
                "resource_id": data["evidence"]["sg-rule"]["resource_id"],
                "configuration_path": "VpcId",
                "reason": "Unknown value",
            }
        ],
    )
    data["evidence"]["sg-rule"].update(
        value=None,
        value_status="unresolved",
        limitation="Value unavailable",
    )
    path = save_snapshot(data, tmp_path / "snapshot.json")
    loaded = load_snapshot(path)
    unresolved = loaded.coverage.operations[2].unresolved_values[0]
    assert unresolved.resource_id == data["evidence"]["sg-rule"]["resource_id"]
    assert unresolved.configuration_path == "VpcId"
    assert unresolved.reason == "Unknown value"
    assert loaded.evidence["sg-rule"].value is None
    assert loaded.evidence["sg-rule"].value_status == "unresolved"
    assert loaded.evidence["sg-rule"].limitation == "Value unavailable"


@pytest.mark.parametrize("defect", ["identity", "reference", "serialization"])
def test_invalid_mutations_never_create_output(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    defect: str,
) -> None:
    if defect == "identity":
        instance(snapshot).identity.physical_id = "i-other"
    elif defect == "reference":
        instance(snapshot).fact_evidence["/instance_type"] = ["missing"]
    else:
        instance(snapshot).instance_type = object()
    path = tmp_path / "snapshot.json"
    with pytest.raises(SnapshotValidationError) as error:
        save_snapshot(snapshot, path)
    assert error.value.path == path
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing", ["valid", "invalid", "directory", "symlink", "dangling"])
def test_save_refuses_every_existing_destination(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    existing: str,
) -> None:
    path = tmp_path / "snapshot.json"
    target = tmp_path / "target.json"
    if existing == "valid":
        save_snapshot(snapshot, path)
    elif existing == "invalid":
        path.write_bytes(b"not JSON")
    elif existing == "directory":
        path.mkdir()
    else:
        if existing == "symlink":
            target.write_bytes(b"original")
        path.symlink_to(target)
    original = path.read_bytes() if path.is_file() else None
    with pytest.raises(SnapshotConflictError) as error:
        save_snapshot(snapshot, path)
    assert error.value.code == "snapshot_conflict"
    assert error.value.path == path
    if original is not None:
        assert path.read_bytes() == original
    if existing in {"symlink", "dangling"}:
        assert path.is_symlink()
        assert path.readlink() == target
    assert not list(tmp_path.glob(".archdiscov-*.tmp"))


def test_missing_parent_is_not_created(snapshot: ArchitectureSnapshot, tmp_path: Path) -> None:
    path = tmp_path / "missing" / "snapshot.json"
    with pytest.raises(SnapshotIOError) as error:
        save_snapshot(snapshot, path)
    assert error.value.path == path
    assert not path.parent.exists()


@pytest.mark.parametrize("operation", ["create", "write", "flush", "fsync", "link"])
def test_precommit_faults_leave_no_output(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = tmp_path / "snapshot.json"

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "private-secret")

    if operation == "create":
        monkeypatch.setattr(publication.tempfile, "NamedTemporaryFile", fail)
    elif operation in {"write", "flush"}:
        original = publication.tempfile.NamedTemporaryFile

        def temporary(*args, **kwargs):
            handle = original(*args, **kwargs)
            if operation == "write":
                write = handle.write

                def partial_write(text):
                    write(text[:20])
                    fail()

                handle.write = partial_write
            else:
                handle.flush = fail
            return handle

        monkeypatch.setattr(publication.tempfile, "NamedTemporaryFile", temporary)
    else:
        monkeypatch.setattr(publication.os, operation, fail)
    with pytest.raises(SnapshotIOError) as error:
        save_snapshot(snapshot, path)
    assert error.value.__cause__ is not None
    assert "private-secret" not in str(error.value)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_unsupported_hard_links_fail_safely(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "Hard links unsupported")

    monkeypatch.setattr(publication.os, "link", unsupported)
    with pytest.raises(SnapshotIOError, match="hard-link support"):
        save_snapshot(snapshot, tmp_path / "snapshot.json")
    assert list(tmp_path.iterdir()) == []


def test_publication_handles_a_destination_created_during_save(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = os.link
    path = tmp_path / "snapshot.json"

    def racing_link(source, destination):
        path.write_bytes(b"other writer")
        link(source, destination)

    monkeypatch.setattr(publication.os, "link", racing_link)
    with pytest.raises(SnapshotConflictError):
        save_snapshot(snapshot, path)
    assert path.read_bytes() == b"other writer"
    assert sorted(tmp_path.iterdir()) == [path]


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


@pytest.mark.parametrize("committed", [True, False])
def test_cleanup_failure_respects_commit_point(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    committed: bool,
) -> None:
    path = tmp_path / "snapshot.json"

    def fail_unlink(*args, **kwargs):
        raise PermissionError("cleanup failed")

    def fail_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        if committed:
            assert save_snapshot(snapshot, path) == path
            assert load_snapshot(path) == snapshot
        else:
            patch.setattr(publication.os, "link", fail_link)
            with pytest.raises(SnapshotIOError) as error:
                save_snapshot(snapshot, path)
            assert error.value.__cause__.errno == errno.EOPNOTSUPP
            assert not path.exists()
    assert f"committed={committed}" in caplog.text
    leftovers = list(tmp_path.glob(".archdiscov-*.tmp"))
    assert len(leftovers) == 1
    for leftover in leftovers:
        leftover.unlink()


def test_mapping_inputs_detach_nested_mutable_models(snapshot: ArchitectureSnapshot) -> None:
    data = snapshot.model_dump(mode="json")
    data["regions"] = snapshot.regions
    instance(snapshot).attributes["password"] = "private-secret"
    validated = validate_snapshot(data)
    assert instance(validated) is not instance(snapshot)
    assert instance(validated).attributes["password"] == "[REDACTED]"
    assert instance(snapshot).attributes["password"] == "private-secret"


def test_mapping_nested_models_cannot_bypass_validation(snapshot: ArchitectureSnapshot) -> None:
    data = snapshot.model_dump(mode="json")
    data["regions"] = snapshot.regions
    instance(snapshot).instance_type = 123
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(data)


def test_mutated_model_schema_version_has_distinct_code(snapshot: ArchitectureSnapshot) -> None:
    snapshot.schema_version = "4"
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(snapshot)
    assert error.value.code == "unsupported_schema_version"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "\ud800"])
def test_unserializable_json_values_do_not_create_files(
    snapshot: ArchitectureSnapshot,
    tmp_path: Path,
    value: object,
) -> None:
    snapshot.metadata["ordinary"] = value
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
