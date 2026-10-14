import asyncio
from pathlib import Path

import pytest

from archdiscov.ingestion.async_collection import AccountCollection
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.services import (
    SnapshotValidationError,
    async_ingestion,
    collect_and_save,
    collect_and_save_async,
    snapshots,
)


@pytest.fixture
def snapshot():
    path = Path(__file__).parent / "fixtures" / "snapshots" / "hierarchy.json"
    return ArchitectureSnapshot.model_validate_json(path.read_text())


def instance(snapshot):
    return snapshot.regions["eu-west-1"].resources[0].children[0].children[0]


def save_account(snapshot, path, *, asynchronous=True):
    request = AwsCollectionRequest(
        accounts=(
            AwsAccountConfig(
                profile="test",
                expected_account_id=snapshot.account_id,
                regions=("eu-west-1",),
                services=("ec2",),
            ),
        )
    )

    class Adapter:
        async def collect_accounts_async(self, request, *, context):
            return [AccountCollection(snapshot.account_id, snapshot)]

    if asynchronous:
        return asyncio.run(collect_and_save_async(request, [path], adapter=Adapter()))[0]
    return collect_and_save(request, [path], adapter=Adapter())[0]


@pytest.mark.parametrize("asynchronous", [True, False])
def test_account_save_validates_once_and_publishes_detached_snapshot(
    snapshot, tmp_path, monkeypatch, asynchronous
):
    instance(snapshot).attributes["password"] = "private-secret"
    calls = []
    validate = snapshots.validate_snapshot

    def counted(data):
        calls.append(data)
        return validate(data)

    monkeypatch.setattr(snapshots, "validate_snapshot", counted)
    monkeypatch.setattr(async_ingestion, "validate_snapshot", counted)
    path = tmp_path / "snapshot.json"
    outcome = save_account(snapshot, path, asynchronous=asynchronous)

    assert outcome.error is None
    assert outcome.path == path
    assert calls == [snapshot]
    assert outcome.snapshot is not snapshot
    assert instance(outcome.snapshot) is not instance(snapshot)
    assert instance(outcome.snapshot).attributes["password"] == "[REDACTED]"
    assert instance(snapshot).attributes["password"] == "private-secret"
    assert "private-secret" not in path.read_text()
    assert snapshots.load_snapshot(path) == outcome.snapshot


def test_public_save_rejects_mutated_previously_validated_model(snapshot, tmp_path):
    validated = snapshots.validate_snapshot(snapshot)
    instance(validated).id = "invalid"
    path = tmp_path / "snapshot.json"

    with pytest.raises(SnapshotValidationError) as error:
        snapshots.save_snapshot(validated, str(path))

    assert error.value.path == path
    assert error.value.code == "invalid_snapshot"
    assert error.value.__cause__ is not None
    assert not list(tmp_path.iterdir())


def test_account_validation_error_has_output_path(snapshot, tmp_path):
    instance(snapshot).id = "invalid"
    path = tmp_path / "snapshot.json"
    outcome = save_account(snapshot, path)

    assert isinstance(outcome.error, SnapshotValidationError)
    assert outcome.error.path == path
    assert outcome.error.__cause__ is not None
    assert outcome.snapshot is None
    assert outcome.path is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "\ud800"])
def test_account_serialization_error_has_output_path(snapshot, tmp_path, value):
    snapshot.metadata["ordinary"] = value
    path = tmp_path / "snapshot.json"
    outcome = save_account(snapshot, path)

    assert isinstance(outcome.error, SnapshotValidationError)
    assert outcome.error.path == path
    assert outcome.error.details[0].type == "serialization_error"
    assert outcome.error.__cause__ is not None
    assert outcome.path is None
    assert not list(tmp_path.iterdir())
