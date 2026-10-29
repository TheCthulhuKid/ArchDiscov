import asyncio
from threading import Event

import pytest

from archdiscov.ingestion import aws
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.async_collection import AccountCollection
from archdiscov.services import (
    SnapshotConflictError,
    SnapshotValidationError,
    async_ingestion,
    collect_and_save,
    collect_and_save_async,
    collect_snapshots,
    collect_snapshots_async,
    load_snapshot,
)
from tests.support.async_collection import ACCOUNT, OTHER, account, context, empty_job
from tests.support.aws_fakes import Session
from tests.support.ingestion_service import context as simple_context
from tests.support.ingestion_service import request


def test_async_apis_work_in_existing_loop_with_ordered_isolated_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "collect_job", empty_job)
    req = request().model_copy(update={"accounts": (account(), account(OTHER))})
    ctx = context(lambda **kw: Session(OTHER))
    paths = [tmp_path / "first.json", tmp_path / "second.json"]

    async def run():
        outcomes = await collect_and_save_async(req, paths, context=ctx)
        assert [result.account_id for result in outcomes] == [ACCOUNT, OTHER]
        assert outcomes[0].error.code == "account_mismatch"
        assert outcomes[0].snapshot is None and not paths[0].exists()
        assert outcomes[1].error is None and outcomes[1].path == paths[1]
        assert load_snapshot(paths[1]) == outcomes[1].snapshot
        before = paths[1].read_bytes()
        again = await collect_and_save_async(req, paths, context=ctx)
        assert isinstance(again[1].error, SnapshotConflictError)
        assert paths[1].read_bytes() == before
        with pytest.raises(IngestionError, match="account"):
            await collect_snapshots_async(req, context=ctx)

    asyncio.run(run())


def test_async_and_sync_collection_return_equivalent_detached_snapshots():
    sync = collect_snapshots(request(), context=simple_context())
    result = asyncio.run(collect_snapshots_async(request(), context=simple_context()))
    assert result == sync


def test_sync_api_fails_actionably_inside_loop_before_adapter_calls(tmp_path):
    async def run():
        with pytest.raises(IngestionError, match=r"async.*running event loop") as exc:
            collect_and_save(request(), [tmp_path / "unused.json"], context=simple_context())
        assert exc.value.code == "invalid_request"

    asyncio.run(run())
    assert not list(tmp_path.iterdir())


class LegacyAdapter:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.requests = []

    def collect(self, request, *, context):
        self.requests.append(request)
        return [self.snapshot]


def test_sync_only_injected_adapter_remains_supported(tmp_path):
    (snapshot,) = collect_snapshots(request(), context=simple_context())
    adapter = LegacyAdapter(snapshot)
    (detached,) = asyncio.run(collect_snapshots_async(request(), adapter=adapter))
    assert detached == snapshot and detached is not snapshot
    assert adapter.requests == [request()]
    outcome = collect_and_save(request(), [tmp_path / "legacy.json"], adapter=adapter)[0]
    assert outcome.error is None


def test_async_duplicate_paths_are_rejected_before_collection(tmp_path):
    class NeverCalled:
        def collect(self, *args, **kwargs):
            pytest.fail("invalid output paths must not collect")

    req = request().model_copy(update={"accounts": (account(), account(OTHER))})
    with pytest.raises(IngestionError, match="distinct"):
        asyncio.run(
            collect_and_save_async(
                req, [tmp_path / "a", tmp_path / "." / "a"], adapter=NeverCalled()
            )
        )


def test_invalid_snapshot_isolated_from_successful_account_save(tmp_path):
    (valid,) = collect_snapshots(request(), context=simple_context())
    invalid = valid.model_copy(deep=True)
    invalid.account_id = OTHER
    req = request().model_copy(update={"accounts": (account(), account(OTHER))})

    class NativeAdapter:
        async def collect_accounts_async(self, request, *, context):
            return [AccountCollection(ACCOUNT, valid), AccountCollection(OTHER, invalid)]

    paths = [tmp_path / "valid.json", tmp_path / "invalid.json"]
    outcomes = asyncio.run(collect_and_save_async(req, paths, adapter=NativeAdapter()))
    assert outcomes[0].error is None
    assert load_snapshot(paths[0]) == valid
    assert isinstance(outcomes[1].error, SnapshotValidationError)
    assert not paths[1].exists()


def test_cancellation_preserves_earlier_commits_and_does_not_start_later_saves(
    monkeypatch, tmp_path
):
    entered, release = Event(), Event()
    save = async_ingestion._save_validated_snapshot
    ids = (ACCOUNT, OTHER, "111111111111")
    req = request().model_copy(
        update={"accounts": tuple(account(value) for value in ids), "max_active_accounts": 1}
    )
    paths = [tmp_path / f"{value}.json" for value in ids]
    monkeypatch.setattr(aws, "collect_job", empty_job)

    def blocking_save(snapshot, path):
        if snapshot.account_id == OTHER:
            entered.set()
            assert release.wait(5)
        return save(snapshot, path)

    monkeypatch.setattr(async_ingestion, "_save_validated_snapshot", blocking_save)

    async def run():
        task = asyncio.create_task(collect_and_save_async(req, paths, context=context()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            before = paths[0].read_bytes()
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert paths[0].read_bytes() == before

    asyncio.run(run())
    assert load_snapshot(paths[1]).account_id == OTHER
    assert not paths[2].exists()
