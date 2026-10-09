import asyncio
from threading import Event

import pytest

from archdiscov.ingestion.async_collection import AsyncCollectionCoordinator
from archdiscov.ingestion.concurrency import SdkCallBudget
from archdiscov.ingestion.requests import AwsCollectionRequest

from .test_async_collection import account, context


def test_cancelled_collector_cannot_start_next_sdk_call_and_budget_is_reusable():
    entered, release = Event(), Event()
    req = AwsCollectionRequest(accounts=(account(),), max_collector_jobs=1)
    calls = []
    ctx = context().with_call_budget(1)

    async def run():
        coordinator = AsyncCollectionCoordinator(req, ctx, lambda **kwargs: object())

        def collect():
            with coordinator.context.sdk_calls.permit(account().expected_account_id, 1):
                calls.append("first")
                entered.set()
                assert release.wait(5)
            with coordinator.context.sdk_calls.permit(account().expected_account_id, 1):
                calls.append("second")

        async def collect_account(*args):
            await coordinator.blocking(collect)

        task = asyncio.create_task(coordinator.run(collect_account))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            # Process cancellation through both scheduling and thread boundaries.
            for _ in range(12):
                await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls == ["first"]
    with ctx.sdk_calls.permit(account().expected_account_id, 1):
        assert ctx.sdk_calls._active == 1
    assert ctx.sdk_calls._active == 0


def test_cancelled_paginator_retains_current_call_but_reads_no_later_page():
    from archdiscov.ingestion.concurrency import CancellableSdkCalls

    budget = SdkCallBudget(1)
    gate = CancellableSdkCalls(budget)
    calls = []

    def pages():
        calls.append(1)
        yield {"items": [1]}
        calls.append(2)
        yield {"items": [2]}

    guarded = gate.pages(pages(), account().expected_account_id, 1)
    assert next(guarded) == {"items": [1]}
    gate.cancel()
    with pytest.raises(asyncio.CancelledError):
        next(guarded)
    assert calls == [1]
    assert budget._active == 0
