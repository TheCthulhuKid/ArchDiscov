"""Account configurations, a collection context, and a job that collects nothing."""

from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.collection_jobs import CollectedJob
from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.requests import AwsAccountConfig
from tests.support.aws_fakes import Session

ACCOUNT = "123456789012"
OTHER = "999999999999"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def account(account_id=ACCOUNT, **kwargs):
    return AwsAccountConfig(
        profile=account_id, expected_account_id=account_id, regions=("us-east-1",), **kwargs
    )


def context(factory=None):
    return AdapterContext(
        clock=lambda: NOW,
        uuid_factory=lambda: UUID(int=175),
        session_factory=factory or (lambda **kw: Session(kw["profile_name"])),
    )


def empty_job(job, *args):
    return CollectedJob(job, ServiceCollection([], {}, []))
