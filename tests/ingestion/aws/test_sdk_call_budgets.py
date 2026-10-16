from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Condition, Event, Lock

import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import OperationRunner, OperationTask, create_session
from archdiscov.ingestion.concurrency import SdkCallBudget
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.identity import verify_account_identity
from archdiscov.ingestion.region_scope import resolve_region_scope
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services import collect_and_save

ACCOUNT = "123456789012"
OTHER = "999999999999"


def config(account_id=ACCOUNT, **kwargs):
    return AwsAccountConfig(
        profile="dev", expected_account_id=account_id, regions=("us-east-1",), **kwargs
    )


class ObservedBudget(SdkCallBudget):
    def __init__(self, limit, attempts):
        super().__init__(limit)
        self.attempts = attempts
        self.lock = Lock()
        self.waiting = Event()

    @contextmanager
    def permit(self, account_id, limit):
        with self.lock:
            self.attempts -= 1
            if self.attempts == 0:
                self.waiting.set()
        with super().permit(account_id, limit):
            yield


class Calls:
    def __init__(self):
        self.condition = Condition()
        self.release = Event()
        self.active = Counter()
        self.maximum = Counter()
        self.total_maximum = 0
        self.entered = 0

    def invoke(self, account_id):
        with self.condition:
            self.active[account_id] += 1
            self.maximum[account_id] = max(self.maximum[account_id], self.active[account_id])
            self.total_maximum = max(self.total_maximum, self.active.total())
            self.entered += 1
            self.condition.notify_all()
        try:
            assert self.release.wait(10), "test did not release the SDK call"
        finally:
            with self.condition:
                self.active[account_id] -= 1


class Client:
    def __init__(self, calls, account_id):
        self.calls = calls
        self.account_id = account_id

    def can_paginate(self, method):
        return True

    def get_paginator(self, method):
        return self

    def paginate(self, **params):
        for index in range(2):
            self.calls.invoke(self.account_id)
            yield {"Items": [index]}

    def get_role(self, **params):
        self.calls.invoke(self.account_id)
        if params["RoleName"] == "bad":
            raise EndpointConnectionError(endpoint_url="protected-secret")
        return {"Role": params["RoleName"]}


class Session:
    def __init__(self, client):
        self.obj = client

    def client(self, name, **kwargs):
        return self.obj


@pytest.mark.parametrize("global_limit,account_limit,capacity", [(1, 4, 1), (4, 1, 2), (3, 2, 3)])
def test_nested_operation_and_detail_pools_share_total_and_account_limits(
    global_limit, account_limit, capacity
):
    calls = Calls()
    # Four operation workers plus eight detail workers must all try the shared budget.
    budget = ObservedBudget(global_limit, attempts=12)
    context = AdapterContext(call_budget=budget)
    jobs = []
    for account_id in (ACCOUNT, OTHER):
        account = config(account_id, max_in_flight_calls=account_limit)
        session = Session(Client(calls, account_id))
        operations = OperationRunner(session, account, context)
        tasks = [
            OperationTask(AwsRegion.US_EAST_1, AwsService.EC2, op, result_key="Items")
            for op in ("DescribeVpcs", "DescribeSubnets")
        ]
        details = DetailRunner(
            session,
            account,
            context,
            service=AwsService.IAM,
            region=AwsRegion.GLOBAL,
            client_name="iam",
        )
        batch = [DetailCall("GetRole", {"RoleName": name}) for name in ("a", "b", "c", "bad")]
        jobs.extend(
            [
                lambda r=operations, t=tasks: r.run_many(t),
                lambda r=details, b=batch: r.run("GetRole", b),
            ]
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(job) for job in jobs]
        try:
            assert budget.waiting.wait(10), "nested workers did not attempt their calls"
            with calls.condition:
                assert calls.condition.wait_for(lambda: calls.active.total() >= capacity, 10)
                assert calls.active.total() == capacity
        finally:
            calls.release.set()
        results = [future.result(timeout=10) for future in futures]
    assert calls.total_maximum == capacity
    assert all(value <= account_limit for value in calls.maximum.values())
    assert calls.active.total() == 0
    assert calls.entered == 16
    for operations, details in zip(results[::2], results[1::2], strict=True):
        assert all(result.items == [0, 1] for result in operations)
        assert all(result.coverage.result_count == 2 for result in operations)
        assert details.coverage.status == "incomplete"
        assert details.coverage.result_count == 3
        assert "protected-secret" not in details.coverage.model_dump_json()
    # Failed details must not leave capacity behind.
    with budget.permit(ACCOUNT, account_limit):
        pass


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_permits_release_even_on_base_exceptions(failure):
    budget = SdkCallBudget(1)
    with pytest.raises(failure):
        with budget.permit(ACCOUNT, 1):
            raise failure()
    with ThreadPoolExecutor(max_workers=1) as pool:

        def reuse():
            with budget.permit(OTHER, 1):
                return True

        assert pool.submit(reuse).result(timeout=5)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_budget_rejects_invalid_limits(limit):
    with pytest.raises(ValueError, match="positive integer"):
        SdkCallBudget(limit)
    with pytest.raises(ValueError, match="positive integer"):
        SdkCallBudget().restrict(limit)
    with pytest.raises(ValueError, match="positive integer"):
        with SdkCallBudget().permit(ACCOUNT, limit):
            pytest.fail("invalid account limit accepted")


def test_account_limit_cannot_change_within_shared_budget():
    budget = SdkCallBudget()
    with budget.permit(ACCOUNT, 1):
        pass
    with pytest.raises(ValueError, match="consistent"):
        with budget.permit(ACCOUNT, 2):
            pytest.fail("inconsistent account limit accepted")


def test_paginator_releases_capacity_before_page_processing_and_after_failure():
    budget = SdkCallBudget(1)

    def source():
        yield {"Items": ["retained"]}
        raise RuntimeError("later page failed")

    pages = budget.pages(source(), ACCOUNT, 1)
    assert next(pages) == {"Items": ["retained"]}

    def process():
        # The suspended paginator must not reserve capacity during normalization.
        with budget.permit(ACCOUNT, 1):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(process).result(timeout=5)
        with pytest.raises(RuntimeError, match="later page failed"):
            next(pages)
        assert pool.submit(process).result(timeout=5)


@pytest.mark.parametrize("operation", ["identity", "discovery", "assume_role"])
@pytest.mark.parametrize("failed", [False, True])
def test_identity_discovery_and_assume_role_share_budget(operation, failed):
    calls = Calls()

    class IdentityClient:
        def invoke(self):
            calls.invoke(ACCOUNT)
            if failed:
                raise EndpointConnectionError(endpoint_url="protected-secret")

        def get_caller_identity(self):
            self.invoke()
            return {"Account": ACCOUNT}

        def describe_regions(self, **params):
            self.invoke()
            return {"Regions": [{"RegionName": "us-east-1", "OptInStatus": "opted-in"}]}

        def assume_role(self, **params):
            self.invoke()
            return {
                "Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}
            }

    budget = ObservedBudget(1, attempts=2)
    session = Session(IdentityClient())
    context = AdapterContext(call_budget=budget, session_factory=lambda **kwargs: session)
    account = config()
    if operation == "identity":

        def job():
            return verify_account_identity(session, account, context=context)
    elif operation == "discovery":
        account = AwsAccountConfig(
            profile="dev",
            expected_account_id=ACCOUNT,
            region_scope="enabled",
            bootstrap_region="us-east-1",
        )

        def job():
            return resolve_region_scope(session, account, partition="aws", context=context)
    else:
        account = config(role_arn=f"arn:aws:iam::{ACCOUNT}:role/Reader")

        def job():
            return create_session(account, context)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with budget.permit(OTHER, 1):
            future = pool.submit(job)
            try:
                assert budget.waiting.wait(10)
                assert calls.entered == 0
            finally:
                calls.release.set()
        if failed:
            with pytest.raises(IngestionError) as error:
                future.result(timeout=10)
            assert "protected-secret" not in str(error.value)
        else:
            future.result(timeout=10)
    assert calls.entered == 1
    with budget.permit(OTHER, 1):
        pass


def test_context_fallback_is_shared_and_request_budget_preserves_seams():
    context = AdapterContext()
    assert context.sdk_calls is context.sdk_calls
    bound = context.with_call_budget(2)
    assert bound.clock is context.clock
    assert bound.uuid_factory is context.uuid_factory
    assert bound.with_call_budget(2) is bound
    assert bound.sdk_calls is bound.call_budget


def test_injected_budget_cannot_bypass_request_limit():
    budget = ObservedBudget(4, attempts=2)
    context = AdapterContext(call_budget=budget).with_call_budget(1)
    assert context.call_budget is budget
    # A later subrequest must not widen the shared ceiling again.
    context.with_call_budget(4)
    entered = Event()

    def call():
        with budget.permit(OTHER, 4):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with budget.permit(ACCOUNT, 4):
            future = pool.submit(call)
            assert budget.waiting.wait(10)
            assert not entered.is_set()
        future.result(timeout=10)
    assert entered.is_set()


def test_persistence_subrequests_preserve_limits_and_one_budget(tmp_path):
    seen = []

    class Adapter:
        def collect(self, request, *, context):
            seen.append((request, context))
            raise IngestionError("identity_failed", "expected test failure")

    request = AwsCollectionRequest(
        accounts=(config(), config(OTHER)),
        max_in_flight_calls=1,
        max_active_accounts=1,
        max_collector_jobs=2,
    )
    outcomes = collect_and_save(
        request, [tmp_path / "a.json", tmp_path / "b.json"], adapter=Adapter()
    )
    assert len(outcomes) == 2
    assert seen[0][1].call_budget is seen[1][1].call_budget
    for subrequest, _ in seen:
        assert subrequest.max_in_flight_calls == 1
        assert subrequest.max_active_accounts == 1
        assert subrequest.max_collector_jobs == 2
    assert list(tmp_path.iterdir()) == []
