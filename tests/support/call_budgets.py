"""An SDK call budget that records its permits, and a session whose clients count their calls."""

from collections import Counter
from contextlib import contextmanager
from threading import Condition, Event, Lock

from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.concurrency import SdkCallBudget


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
