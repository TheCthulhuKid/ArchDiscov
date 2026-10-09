from typing import Any

from ...models.enums import AwsRegion, AwsService
from ..adapter import AdapterContext
from ..detail_execution import DetailCall as IamCall
from ..detail_execution import DetailResponse as IamResponse
from ..detail_execution import DetailResult as IamResult
from ..detail_execution import DetailRunner
from ..requests import AwsAccountConfig

__all__ = ["IamCall", "IamResponse", "IamResult", "IamRunner"]


class IamRunner(DetailRunner):
    """Run bounded, account-wide IAM reads with request-specific evidence locations."""

    def __init__(self, session: Any, account: AwsAccountConfig, context: AdapterContext) -> None:
        super().__init__(
            session,
            account,
            context,
            service=AwsService.IAM,
            region=AwsRegion.GLOBAL,
            client_name="iam",
            pagination="iam",
        )
