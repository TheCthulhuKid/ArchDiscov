import re

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..models.architecture import CollectionFailure
from ..models.enums import AwsService
from .adapter import IngestionError
from .operations import ALLOWED_OPERATIONS
from .requests import AwsAccountConfig

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def client_config(account: AwsAccountConfig) -> Config:
    return Config(
        connect_timeout=account.connect_timeout_seconds,
        read_timeout=account.read_timeout_seconds,
        retries={"max_attempts": account.max_attempts, "mode": "standard"},
    )


def require_allowed(service: AwsService, operation: str) -> None:
    if (service, operation) not in ALLOWED_OPERATIONS:
        raise IngestionError(
            "operation_not_allowed", f"operation {service.value}.{operation} is not allowlisted"
        )


def sanitize_failure(error: BaseException, operation: str) -> CollectionFailure:
    """Describe a failure without any raw exception text, which may contain secrets."""
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        if not _SAFE_CODE.match(code):
            code = "ClientError"
    elif isinstance(error, BotoCoreError):
        code = type(error).__name__
    else:
        code = "UnexpectedError"
    return CollectionFailure(code=code, message=f"{operation} failed with {code}")
