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
    snapshots,
)

from .test_async_collection import ACCOUNT, OTHER, account, context, empty_job
from .test_ec2_collection import Session, default_pages, make_context
from .test_ingestion_service import context as simple_context
from .test_ingestion_service import request


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


@pytest.mark.parametrize("api", [collect_snapshots, collect_and_save, aws.AwsAdapter().collect])
def test_sync_apis_fail_actionably_inside_loop_before_adapter_calls(api, tmp_path):
    async def run():
        kwargs = {"context": simple_context()}
        args = [request()]
        if api is collect_and_save:
            args.append([tmp_path / "unused.json"])
        with pytest.raises(IngestionError, match="async.*running event loop") as exc:
            api(*args, **kwargs)
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


def test_sync_only_injected_adapter_remains_supported_and_revalidated(tmp_path):
    (snapshot,) = collect_snapshots(request(), context=simple_context())
    adapter = LegacyAdapter(snapshot)
    (detached,) = asyncio.run(collect_snapshots_async(request(), adapter=adapter))
    assert detached == snapshot and detached is not snapshot
    assert adapter.requests == [request()]
    outcome = collect_and_save(request(), [tmp_path / "legacy.json"], adapter=adapter)[0]
    assert outcome.error is None
    snapshot.account_id = OTHER
    with pytest.raises(SnapshotValidationError):
        asyncio.run(collect_snapshots_async(request(), adapter=adapter))


@pytest.mark.parametrize("outputs", [[], ["same.json", "same.json"]])
def test_async_paths_checked_before_collection(tmp_path, outputs):
    class NeverCalled:
        def collect(self, *args, **kwargs):
            pytest.fail("invalid output paths must not collect")

    with pytest.raises(IngestionError, match="output"):
        asyncio.run(
            collect_and_save_async(
                request(), [tmp_path / path for path in outputs], adapter=NeverCalled()
            )
        )


def test_async_duplicate_paths_are_rejected_for_matching_account_count(tmp_path):
    req = request().model_copy(update={"accounts": (account(), account(OTHER))})
    with pytest.raises(IngestionError, match="distinct"):
        asyncio.run(collect_and_save_async(req, [tmp_path / "a", tmp_path / "." / "a"]))


def test_native_adapter_output_mapping_is_checked_before_publication(tmp_path):
    class WrongOrder:
        async def collect_accounts_async(self, request, *, context):
            return [AccountCollection(OTHER, error=IngestionError("identity_failed", "safe"))]

    with pytest.raises(IngestionError, match="invalid account outcomes"):
        asyncio.run(collect_and_save_async(request(), [tmp_path / "a"], adapter=WrongOrder()))
    assert not list(tmp_path.iterdir())


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


@pytest.mark.parametrize("stage", ["before_link", "after_link"])
def test_cancelled_save_settles_publication_and_preserves_committed_files(
    monkeypatch, tmp_path, stage
):
    started, release = Event(), Event()
    link = snapshots.os.link
    first, second = tmp_path / "first.json", tmp_path / "never.json"
    req = request().model_copy(
        update={"accounts": (account(), account(OTHER)), "max_active_accounts": 1}
    )
    monkeypatch.setattr(aws, "collect_job", empty_job)

    def blocked_link(source, destination):
        if stage == "after_link":
            link(source, destination)
        started.set()
        assert release.wait(5)
        if stage == "before_link":
            link(source, destination)

    monkeypatch.setattr(snapshots.os, "link", blocked_link)

    async def run():
        task = asyncio.create_task(collect_and_save_async(req, [first, second], context=context()))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert first.exists() == (stage == "after_link")
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert load_snapshot(first).account_id == ACCOUNT
    assert not second.exists()
    assert list(tmp_path.iterdir()) == [first]


def test_cancellation_preserves_earlier_commits_and_does_not_start_later_saves(
    monkeypatch, tmp_path
):
    entered, release = Event(), Event()
    save = async_ingestion.save_snapshot
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

    monkeypatch.setattr(async_ingestion, "save_snapshot", blocking_save)

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


def test_async_collection_retains_evidence_and_partial_coverage_on_disk(tmp_path):
    def pages(region):
        result = default_pages(region)
        result["describe_route_tables"] = [RuntimeError("safe injected failure")]
        return result

    ctx = make_context(pages)
    path = tmp_path / "architecture.json"
    (outcome,) = asyncio.run(collect_and_save_async(request(), [path], context=ctx))
    assert outcome.error is None
    saved = load_snapshot(path)
    assert saved == outcome.snapshot
    assert saved.evidence
    assert saved.regions["us-east-1"].resources
    assert not outcome.complete
    assert "hunter2" not in path.read_text()
