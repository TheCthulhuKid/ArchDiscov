"""Explicit context for offline template declarations, never deployment requests."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from ...models.enums import AwsRegion
from ..requests import NonBlank, SourceRequest


class CloudFormationRequest(SourceRequest):
    kind: Literal["cloudformation"] = "cloudformation"
    path: Path
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: AwsRegion
    stack_name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
    stack_id: NonBlank | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    sensitive_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if self.region == AwsRegion.GLOBAL:
            raise ValueError("region must name a concrete AWS region")
        if self.stack_id is not None:
            parts = self.stack_id.split(":", 5)
            if (
                len(parts) != 6
                or parts[0] != "arn"
                or parts[2] != "cloudformation"
                or parts[3] != self.region.value
                or parts[4] != self.account_id
                or not parts[5].startswith("stack/")
                or len(parts[5].split("/")) != 3
                or not all(parts[5].split("/")[1:])
            ):
                raise ValueError("stack_id must be a stack ARN matching account and region")
            if self.stack_name is not None and parts[5].split("/")[1] != self.stack_name:
                raise ValueError("stack_id and stack_name must agree")
        for path in self.sensitive_paths:
            if not path.startswith("/") or any(
                "~" in token.replace("~0", "").replace("~1", "") for token in path.split("/")[1:]
            ):
                raise ValueError("sensitive_paths must contain nonempty JSON Pointers")
        return self
