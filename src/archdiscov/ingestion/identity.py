from dataclasses import dataclass
from typing import Any

from botocore.session import Session

from ..models.enums import AwsService
from .adapter import AdapterContext, IngestionError
from .execution_common import client_config, require_allowed
from .region_scope import authentication_region
from .requests import AwsAccountConfig


@dataclass(frozen=True)
class VerifiedIdentity:
    account_id: str
    partition: str | None


def verify_account_identity(
    session: Any, account: AwsAccountConfig, *, context: AdapterContext | None = None
) -> VerifiedIdentity:
    context = context or AdapterContext()
    require_allowed(AwsService.STS, "GetCallerIdentity")
    try:
        region = authentication_region(session, account)
        client = session.client(
            "sts", config=client_config(account), **({"region_name": region} if region else {})
        )
        with context.sdk_calls.permit(account.expected_account_id, account.max_in_flight_calls):
            identity = client.get_caller_identity()
        actual = identity["Account"]
        if (
            not isinstance(actual, str)
            or len(actual) != 12
            or not actual.isascii()
            or not actual.isdigit()
        ):
            raise ValueError("invalid account identity")
        arn = identity.get("Arn")
        partition = None
        if arn is not None:
            parts = arn.split(":", 5)
            if (
                len(parts) != 6
                or parts[0] != "arn"
                or parts[4] != actual
                or parts[1] not in Session().get_available_partitions()
            ):
                raise ValueError("invalid identity ARN")
            partition = parts[1]
    except Exception as error:
        raise IngestionError("identity_failed", "STS GetCallerIdentity failed") from error
    if actual != account.expected_account_id:
        raise IngestionError(
            "account_mismatch",
            f"session account {actual} does not match expected {account.expected_account_id}",
        )
    return VerifiedIdentity(actual, partition)
