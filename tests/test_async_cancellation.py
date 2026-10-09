import asyncio
from dataclasses import replace
from threading import Event

import pytest

from archdiscov.ingestion import aws
from archdiscov.ingestion.async_collection import AsyncCollectionCoordinator
from archdiscov.ingestion.concurrency import SdkCallBudget
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.models.enums import AwsRegion, AwsService

from .aws_fakes import Session
from .test_async_collection import ACCOUNT, OTHER, account, context, empty_job


async def heartbeat():
    loop = asyncio.get_running_loop()
    for _ in range(12):
        tick = loop.create_future()
        loop.call_soon(tick.set_result, None)
        await tick


def assert_budget_active(budget, count):
    with budget._condition:
        assert budget._active == count
        assert sum(budget._accounts.values()) == count


@pytest.mark.parametrize("stage", ["identity", "collector"])
@pytest.mark.parametrize("cancellations", [1, 3])
def test_adapter_cancellation_drains_blocked_sdk_without_later_work(
    monkeypatch, stage, cancellations
):
    entered = Event()
    release = Event()
    finished = Event()
    sessions = []
    jobs = []
    assembled = []
    returned = []
    budget = SdkCallBudget(1)

    def blocked_call():
        entered.set()
        try:
            assert release.wait(5), "SDK call was not released while the loop was responsive"
        finally:
            finished.set()

    class IdentityClient:
        def get_caller_identity(self):
            blocked_call()
            return {"Account": ACCOUNT}

    class DetailClient:
        def get_role(self, **kwargs):
            blocked_call()
            return {"Role": {"RoleName": kwargs["RoleName"]}}

    class BlockingSession(Session):
        def client(self, name, **kwargs):
            if name == "sts" and stage == "identity":
                return IdentityClient()
            if name == "iam":
                return DetailClient()
            return super().client(name, **kwargs)

    def factory(**kwargs):
        sessions.append(kwargs["profile_name"])
        return BlockingSession(kwargs["profile_name"])

    def collect(job, session, cfg, ctx, *args):
        jobs.append((cfg.expected_account_id, job))
        runner = DetailRunner(
            session,
            cfg,
            ctx,
            service=AwsService.IAM,
            region=AwsRegion.GLOBAL,
            client_name="iam",
        )
        result = runner.run("GetRole", [DetailCall("GetRole", {"RoleName": "blocked"})])
        assert len(result.responses) == 1
        return empty_job(job)

    adapter = aws.AwsAdapter()
    assemble = adapter._assemble_account

    def record_assembly(*args):
        assembled.append(args)
        return assemble(*args)

    monkeypatch.setattr(aws, "collect_job", collect)
    monkeypatch.setattr(adapter, "_assemble_account", record_assembly)
    request = AwsCollectionRequest(
        accounts=(
            account(services=("ec2", "iam"), max_in_flight_calls=1),
            account(OTHER, services=("ec2", "iam"), max_in_flight_calls=1),
        ),
        max_active_accounts=1,
        max_collector_jobs=1,
        max_in_flight_calls=1,
    )

    async def run():
        async def collection():
            outcomes = await adapter.collect_accounts_async(
                request, context=replace(context(factory), call_budget=budget)
            )
            returned.extend(outcomes)

        task = asyncio.create_task(collection())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert_budget_active(budget, 1)
            for _ in range(cancellations):
                assert task.cancel()
                await heartbeat()
                assert not finished.is_set(), "event loop blocked until the SDK call timed out"
                assert not task.done(), "cancellation returned before the SDK call settled"
                assert_budget_active(budget, 1)
                assert sessions == [ACCOUNT]
                assert len(jobs) == (1 if stage == "collector" else 0)
                assert assembled == returned == []
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert task.cancelled()
        assert finished.is_set()
        assert_budget_active(budget, 0)
        assert sessions == [ACCOUNT]
        assert len(jobs) == (1 if stage == "collector" else 0)
        assert assembled == returned == []

    asyncio.run(run())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_coordinator_cancellation_drains_all_workers_and_retains_job_slots(worker_fails):
    entered = [Event(), Event()]
    release = [Event(), Event()]
    finished = [Event(), Event()]
    accounts = []
    jobs = []
    request = AwsCollectionRequest(
        accounts=(account(), account(OTHER)),
        max_active_accounts=1,
        max_collector_jobs=2,
    )

    def job(index):
        jobs.append(index)
        entered[index].set()
        try:
            assert release[index].wait(5), "collector was not released by the event loop"
            if worker_fails and index == 1:
                raise RuntimeError("collector failed during cancellation drain")
            return index
        finally:
            finished[index].set()

    async def collect(cfg, ctx, coordinator):
        accounts.append(cfg.expected_account_id)
        await coordinator.collect_jobs([0, 1, 2], job)
        pytest.fail("cancelled account reached assembly")

    async def run():
        coordinator = AsyncCollectionCoordinator(request, context(), lambda **kwargs: object())
        task = asyncio.create_task(coordinator.run(collect))
        try:
            for event in entered:
                assert await asyncio.to_thread(event.wait, 5)
            task.cancel()
            await heartbeat()
            assert not any(event.is_set() for event in finished)
            assert not task.done()
            assert coordinator._jobs._value == 0
            release[0].set()
            assert await asyncio.to_thread(finished[0].wait, 5)
            await heartbeat()
            task.cancel()
            await heartbeat()
            assert not finished[1].is_set()
            assert not task.done()
            assert coordinator._jobs._value == 1
            assert accounts == [ACCOUNT]
            assert sorted(jobs) == [0, 1]
        finally:
            for event in release:
                event.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert all(event.is_set() for event in finished)
        assert coordinator._jobs._value == 2
        assert accounts == [ACCOUNT]
        assert sorted(jobs) == [0, 1]

    asyncio.run(run())


def test_cancellation_before_scheduled_collection_makes_no_calls(monkeypatch):
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("collection started despite cancellation before scheduling")

    monkeypatch.setattr(aws, "collect_job", unexpected)
    request = AwsCollectionRequest(accounts=(account(), account(OTHER)))

    async def run():
        task = asyncio.create_task(
            aws.AwsAdapter().collect_accounts_async(request, context=context(unexpected))
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await heartbeat()
        assert task.cancelled()
        assert calls == []

    asyncio.run(run())
