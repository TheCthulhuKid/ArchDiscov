from uuid import UUID

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.aws import EC2_OPERATIONS, AwsAdapter
from archdiscov.ingestion.requests import SourceRequest
from archdiscov.services import collect_snapshots, select_adapter
from tests.support.ingestion_service import ACCOUNT, context, request


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
