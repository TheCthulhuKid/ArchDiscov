from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from .cloud_scope import SupportedScope
from .enums import AwsRegion, AwsService, CoverageStatus
from .resource_base import Model, NonEmptyString


class AwsCoverageContext(Model):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: AwsRegion
    service: AwsService


class CloudCoverageContext(Model):
    scope: SupportedScope
    location: NonEmptyString | None = None
    service: NonEmptyString


class CoverageOutcome[Failure, Unresolved](Model):
    operation: NonEmptyString
    status: CoverageStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    result_count: int | None = Field(default=None, ge=0, strict=True)
    failures: list[Failure] = Field(default_factory=list)
    unresolved_values: list[Unresolved] = Field(default_factory=list)
    reason: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("coverage completed_at must not precede started_at")
        if self.status == CoverageStatus.SUCCESS:
            if self.result_count is None or self.failures or self.unresolved_values:
                raise ValueError(
                    "successful coverage requires a count and no failures/unresolved values"
                )
        elif self.status == CoverageStatus.INCOMPLETE:
            if self.result_count is None or not (
                self.failures or self.unresolved_values or self.reason
            ):
                raise ValueError("incomplete coverage requires a retained count and a limitation")
        else:
            if self.result_count is not None:
                raise ValueError("only successful or incomplete coverage can have a result_count")
            if self.status == CoverageStatus.FAILED:
                if not self.failures:
                    raise ValueError("failed coverage requires a failure")
            elif not self.reason or self.failures or self.unresolved_values:
                raise ValueError(
                    "skipped/unsupported coverage requires a reason and no collected data"
                )
        return self
