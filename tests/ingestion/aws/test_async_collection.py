import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from archdiscov.ingestion import aws
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.async_collection import AsyncCollectionCoordinator
from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.models.enums import AwsService
from tests.support.async_collection import ACCOUNT, OTHER, account, context, empty_job
from tests.support.aws_fakes import Session


def test_account_and_job_workflows_overlap_without_changing_request_order(monkeypatch):
    entered = Barrier(2)
    seen = []
    lock = Lock()

    def collect(job, session, cfg, *args):
        entered.wait(5)
        with lock:
            seen.append(cfg.expected_account_id)
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    request = AwsCollectionRequest(accounts=(account(), account(OTHER)), max_collector_jobs=2)
    outcomes = asyncio.run(aws.AwsAdapter().collect_accounts_async(request, context=context()))
    assert set(seen) == {ACCOUNT, OTHER}
    assert [o.account_id for o in outcomes] == [ACCOUNT, OTHER]
    assert all(o.snapshot.account_id == o.account_id and o.error is None for o in outcomes)


def test_scheduling_limits_bound_work_before_worker_submission():
    account_limit, job_limit = 1, 2
    release = Event()
    capacity = Event()
    lock = Lock()
    active = Counter()
    maximum = Counter()
    expected_jobs = min(job_limit, account_limit * 3)
    request = AwsCollectionRequest(
        accounts=(account(), account(OTHER)),
        max_active_accounts=account_limit,
        max_collector_jobs=job_limit,
    )

    def job(value):
        with lock:
            active["jobs"] += 1
            maximum["jobs"] = max(maximum["jobs"], active["jobs"])
            if active["jobs"] == expected_jobs:
                capacity.set()
        try:
            assert release.wait(5)
            return value
        finally:
            with lock:
                active["jobs"] -= 1

    async def collect(cfg, ctx, coordinator):
        with lock:
            active["accounts"] += 1
            maximum["accounts"] = max(maximum["accounts"], active["accounts"])
        try:
            assert await coordinator.collect_jobs([1, 2, 3], job) == [1, 2, 3]
            return None
        finally:
            with lock:
                active["accounts"] -= 1

    async def run():
        coordinator = AsyncCollectionCoordinator(request, context(), lambda **kw: object())
        return await coordinator.run(collect)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, run())
        try:
            assert capacity.wait(5)
            assert maximum["accounts"] <= account_limit
            assert maximum["jobs"] == expected_jobs
        finally:
            release.set()
        assert len(future.result(10)) == 2
    assert not active.total()


def test_identity_failure_isolates_the_account_and_sync_collect_raises_it(monkeypatch):
    seen = []

    def collect(job, session, cfg, *args):
        seen.append(cfg.expected_account_id)
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    request = AwsCollectionRequest(accounts=(account(), account(OTHER)))
    ctx = context(lambda **kw: Session(OTHER))
    outcomes = asyncio.run(aws.AwsAdapter().collect_accounts_async(request, context=ctx))
    assert outcomes[0].error.code == "account_mismatch"
    assert outcomes[0].snapshot is None
    assert outcomes[1].snapshot.account_id == OTHER
    assert seen == [OTHER]
    with pytest.raises(IngestionError) as error:
        aws.AwsAdapter().collect(request, context=ctx)
    assert error.value.code == "account_mismatch"


def test_unexpected_collector_errors_propagate_after_sibling_jobs_settle(monkeypatch):
    barrier = Barrier(2)
    sibling_finished = Event()

    def collect(job, *args):
        barrier.wait(5)
        if job.service is AwsService.EC2:
            raise RuntimeError("collector bug")
        sibling_finished.set()
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    request = AwsCollectionRequest(accounts=(account(services=("ec2", "iam")),))
    with pytest.raises(RuntimeError, match="collector bug"):
        aws.AwsAdapter().collect(request, context=context())
    assert sibling_finished.is_set()


def test_cross_account_session_reuse_is_rejected(monkeypatch):
    session = Session(ACCOUNT)
    monkeypatch.setattr(aws, "collect_job", empty_job)
    request = AwsCollectionRequest(accounts=(account(), account(OTHER)))
    outcomes = asyncio.run(
        aws.AwsAdapter().collect_accounts_async(request, context=context(lambda **kw: session))
    )
    assert outcomes[0].snapshot is not None
    assert outcomes[1].snapshot is None
    assert outcomes[1].error.code == "invalid_request"
    assert "reused a session across accounts" in outcomes[1].error.message
