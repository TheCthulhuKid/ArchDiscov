import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock

import pytest

from archdiscov.ingestion import aws
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.async_collection import AsyncCollectionCoordinator
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.models.enums import AwsService
from tests.support.account_assembly import Session as ArchitectureSession
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


@pytest.mark.parametrize("account_limit,job_limit", [(1, 1), (1, 2), (2, 1), (2, 3)])
def test_scheduling_limits_bound_work_before_worker_submission(account_limit, job_limit):
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


@pytest.mark.parametrize("failure", ["identity", "discovery"])
def test_account_scope_failure_isolated_and_no_jobs_for_failed_account(monkeypatch, failure):
    seen = []

    def collect(job, session, cfg, *args):
        seen.append(cfg.expected_account_id)
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    ctx = context(lambda **kw: Session(OTHER)) if failure == "identity" else context()
    if failure == "discovery":
        resolve = aws.resolve_region_scope

        def discovery(session, cfg, **kwargs):
            if cfg.expected_account_id == ACCOUNT:
                raise IngestionError("region_discovery_failed", "safe discovery failure")
            return resolve(session, cfg, **kwargs)

        monkeypatch.setattr(aws, "resolve_region_scope", discovery)
    request = AwsCollectionRequest(accounts=(account(), account(OTHER)))
    outcomes = asyncio.run(aws.AwsAdapter().collect_accounts_async(request, context=ctx))
    expected = "account_mismatch" if failure == "identity" else "region_discovery_failed"
    assert outcomes[0].error.code == expected
    assert outcomes[0].snapshot is None
    assert outcomes[1].snapshot.account_id == OTHER
    assert seen == [OTHER]


@pytest.mark.parametrize("error", [RuntimeError("collector bug"), ValueError("invalid model")])
def test_unexpected_collector_errors_propagate_after_sibling_jobs_settle(monkeypatch, error):
    barrier = Barrier(2)
    sibling_finished = Event()

    def collect(job, *args):
        barrier.wait(5)
        if job.service is AwsService.EC2:
            raise error
        sibling_finished.set()
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    request = AwsCollectionRequest(accounts=(account(services=("ec2", "iam")),))
    with pytest.raises(type(error), match=str(error)):
        aws.AwsAdapter().collect(request, context=context())
    assert sibling_finished.is_set()


def test_session_preparation_is_serial_but_client_calls_overlap(monkeypatch):
    lock = Lock()
    preparing = False
    client_barrier = Barrier(2)
    calls = []

    class ConcurrentClient:
        def read(self):
            client_barrier.wait(5)

    class UnsafeSession(Session):
        def client(self, name, **kwargs):
            nonlocal preparing
            with lock:
                assert not preparing, "Session client construction overlapped"
                preparing = True
            try:
                if name == "sts":
                    return super().client(name, **kwargs)
                return ConcurrentClient()
            finally:
                with lock:
                    calls.append(name)
                    preparing = False

    def collect(job, session, *args):
        session.client(job.service.value).read()
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    request = AwsCollectionRequest(accounts=(account(services=("ec2", "iam")),))
    aws.AwsAdapter().collect(
        request, context=context(lambda **kw: UnsafeSession(ACCOUNT, pages=lambda region: {}))
    )
    assert Counter(calls) == {"sts": 1, "ec2": 1, "iam": 1}


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


def test_scheduled_nested_detail_pools_share_sdk_budget(monkeypatch):
    from tests.support.call_budgets import Calls, Client, ObservedBudget
    from tests.support.call_budgets import Session as BudgetSession

    calls = Calls()
    budget = ObservedBudget(2, attempts=4)
    ready = Barrier(2)
    cfg = account(services=("ec2", "iam"), max_in_flight_calls=2)
    ctx = context()
    ctx = replace(ctx, call_budget=budget)

    def collect(job, session, config, worker_context, *args):
        ready.wait(5)
        runner = DetailRunner(
            BudgetSession(Client(calls, ACCOUNT)),
            config,
            worker_context,
            service=AwsService.IAM,
            region="global",
            client_name="iam",
        )
        runner.run("GetRole", [DetailCall("GetRole", {"RoleName": str(i)}) for i in range(2)])
        return empty_job(job)

    monkeypatch.setattr(aws, "collect_job", collect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            aws.AwsAdapter().collect, AwsCollectionRequest(accounts=(cfg,)), context=ctx
        )
        try:
            assert budget.waiting.wait(5)
            with calls.condition:
                assert calls.condition.wait_for(lambda: calls.entered == 2, 5)
            assert calls.total_maximum == 2
        finally:
            calls.release.set()
        assert len(future.result(10)) == 1
    assert not calls.active.total()


def _collect_all_services(monkeypatch, real_collect, limit):
    barrier = Barrier(2)

    def collect(job, *args):
        if limit > 1 and job.service in {AwsService.EC2, AwsService.ELASTIC_LOAD_BALANCING}:
            barrier.wait(5)
        return real_collect(job, *args)

    monkeypatch.setattr(aws, "collect_job", collect)
    session = ArchitectureSession(partial=True)
    request = AwsCollectionRequest(accounts=(account(all_services=True),), max_collector_jobs=limit)
    (snapshot,) = aws.AwsAdapter().collect(request, context=context(lambda **kw: session))
    assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
    return snapshot.model_dump(mode="json")


def test_all_service_results_identical_with_sequential_and_parallel_job_limits(monkeypatch):
    real_collect = aws.collect_job
    sequential, parallel = [
        _collect_all_services(monkeypatch, real_collect, limit) for limit in (1, 8)
    ]
    assert sequential == parallel
