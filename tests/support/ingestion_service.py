"""A collection request, and a context whose AWS endpoints cannot be reached."""

from datetime import UTC, datetime
from uuid import UUID

from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest

ACCOUNT = "123456789012"


class Client:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}

    def can_paginate(self, method):
        return True

    def get_paginator(self, method):
        raise EndpointConnectionError(endpoint_url="https://unavailable.test")


class Session:
    def __init__(self, account):
        self.account = account

    def client(self, name, **kwargs):
        return Client(self.account)


def request():
    return AwsCollectionRequest(
        accounts=(
            AwsAccountConfig(
                profile="dev",
                expected_account_id=ACCOUNT,
                regions=("us-east-1",),
                services=("ec2",),
            ),
        )
    )


def context(account=ACCOUNT):
    return AdapterContext(
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
        uuid_factory=lambda: UUID(int=1),
        session_factory=lambda **kwargs: Session(account),
    )
