"""Offline Terraform show-state requests; no backend or provider configuration."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from ...models.enums import AwsRegion
from ..requests import NonBlank, SourceRequest


class TerraformStateRequest(SourceRequest):
    kind: Literal["terraform_state"] = "terraform_state"
    path: Path
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    region: AwsRegion | None = None
    workspace: NonBlank | None = None

    @model_validator(mode="after")
    def concrete_region(self) -> Self:
        if self.region == AwsRegion.GLOBAL:
            raise ValueError("region must name a concrete AWS region")
        return self
