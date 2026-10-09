from datetime import UTC, datetime
from uuid import UUID

import pytest

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import EC2_OPERATIONS, AwsAdapter
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest, SourceRequest
from archdiscov.services import collect_snapshots, select_adapter

ACCOUNT = "123456789012"


class Client:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


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


def test_select_adapter():
    assert isinstance(select_adapter(request()), AwsAdapter)
    with pytest.raises(IngestionError) as exc:
        select_adapter(SourceRequest(kind="other"))
    assert exc.value.code == "invalid_request"


def test_collect_returns_validated_snapshot_with_failed_operations():
    (snapshot,) = collect_snapshots(request(), context=context())
    assert snapshot.account_id == ACCOUNT
    assert snapshot.snapshot_id == UUID(int=1)
    assert snapshot.source.kind == "aws"
    assert snapshot.schema_version == "2"
    assert {o.operation: o.status for o in snapshot.coverage.operations} == {
        operation.value: "failed" for operation, _ in EC2_OPERATIONS
    }
    assert snapshot.regions["us-east-1"].resources == []
    assert snapshot.coverage.account_wide is False


def test_account_mismatch_produces_no_snapshot():
    with pytest.raises(IngestionError) as exc:
        collect_snapshots(request(), context=context("999999999999"))
    assert exc.value.code == "account_mismatch"
