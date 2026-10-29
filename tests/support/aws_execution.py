"""Fake AWS clients, sessions, and clocks, and the operation and detail runners over them."""

import threading
import time
from datetime import UTC, datetime, timedelta

from archdiscov.ingestion import execution_common
from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.aws import OperationRunner, OperationTask
from archdiscov.ingestion.detail_execution import DetailRunner
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService

ACCOUNT = "123456789012"


class Clock:
    def __init__(self):
        self.now = datetime(2025, 1, 1, tzinfo=UTC)
        self.values = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.now += timedelta(seconds=1)
            self.values.append(self.now)
            return self.now


class Session:
    def __init__(self, client):
        self.client_obj = client
        self.calls = []

    def client(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if isinstance(self.client_obj, Exception):
            raise self.client_obj
        return self.client_obj


class FakeClient:
    def __init__(self, pages=None, error=None, delay=0.0, tracker=None):
        self.pages = pages
        self.error = error
        self.delay = delay
        self.tracker = tracker
        self.calls = 0

    def can_paginate(self, method):
        return self.pages is not None

    def get_paginator(self, method):
        self.calls += 1
        return FakePaginator(self.pages)

    def describe_vpcs(self, **params):
        self.calls += 1
        if self.tracker:
            self.tracker.enter()
            time.sleep(self.delay)
            self.tracker.exit()
        if self.error:
            raise self.error
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def get_caller_identity(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {"Account": ACCOUNT}


def operation_runner(client, **cfg):
    session = FakeSession(client)
    return OperationRunner(session, account(**cfg), AdapterContext(clock=Clock())), session


def task(**overrides):
    data = {
        "region": AwsRegion.US_EAST_1,
        "service": AwsService.EC2,
        "operation": "DescribeVpcs",
        "result_key": "Vpcs",
    }
    data.update(overrides)
    return OperationTask(**data)


def detail_runner(client, monkeypatch, **overrides):
    # The collector's operation entries are introduced in the later discovery stage.
    monkeypatch.setattr(
        execution_common,
        "ALLOWED_OPERATIONS",
        execution_common.ALLOWED_OPERATIONS
        | {(AwsService.ELASTIC_LOAD_BALANCING, "DescribeLoadBalancers")},
    )
    session = Session(client)
    account = AwsAccountConfig(
        profile="dev",
        expected_account_id="123456789012",
        regions=("us-east-1",),
        services=(AwsService.ELASTIC_LOAD_BALANCING,),
        **overrides,
    )
    return DetailRunner(
        session,
        account,
        AdapterContext(clock=Clock()),
        service=AwsService.ELASTIC_LOAD_BALANCING,
        region=AwsRegion.US_EAST_1,
        client_name="elbv2",
        pagination="elbv2",
    ), session


def account(**overrides):
    data = {"profile": "dev", "expected_account_id": ACCOUNT, "regions": ["us-east-1"]}
    data.update(overrides)
    return AwsAccountConfig.model_validate(data)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **params):
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            yield page


class FakeSession:
    def __init__(self, client=None, clients=None, kwargs=None):
        self.client_obj = client
        self.clients = clients or {}
        self.kwargs = kwargs or {}
        self.client_calls = []

    def client(self, name, **kwargs):
        self.client_calls.append((name, kwargs))
        return self.clients.get(name, self.client_obj)
