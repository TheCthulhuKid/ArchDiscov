from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..enums import AnalyzerKind
from ..resource_base import Model, NonEmptyString

AnalysisDepth = Annotated[int, Field(ge=1, le=10)]
AnalysisResultLimit = Annotated[int, Field(ge=1, le=10000)]


class OwnershipParameters(Model):
    kind: Literal[AnalyzerKind.OWNERSHIP] = AnalyzerKind.OWNERSHIP
    ownership_keys: list[NonEmptyString] = Field(
        default_factory=lambda: ["Owner", "Team"], min_length=1
    )

    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        if len(self.ownership_keys) != len(set(self.ownership_keys)):
            raise ValueError("ownership_keys must be unique")
        return self


class SecurityGroupImpactParameters(Model):
    kind: Literal[AnalyzerKind.SECURITY_GROUP_IMPACT] = AnalyzerKind.SECURITY_GROUP_IMPACT
    resource_id: NonEmptyString
    max_depth: AnalysisDepth = 3
    result_limit: AnalysisResultLimit = 100


class CrossAccountTrustParameters(Model):
    kind: Literal[AnalyzerKind.CROSS_ACCOUNT_TRUST] = AnalyzerKind.CROSS_ACCOUNT_TRUST
    result_limit: AnalysisResultLimit = 100


class DatabaseAccessParameters(Model):
    kind: Literal[AnalyzerKind.DATABASE_ACCESS] = AnalyzerKind.DATABASE_ACCESS
    resource_id: NonEmptyString
    port: int = Field(ge=1, le=65535)
    result_limit: AnalysisResultLimit = 100


class InternetExposureParameters(Model):
    kind: Literal[AnalyzerKind.INTERNET_EXPOSURE] = AnalyzerKind.INTERNET_EXPOSURE
    max_depth: AnalysisDepth = 3
    result_limit: AnalysisResultLimit = 100


AnalyzerParameters = Annotated[
    OwnershipParameters
    | SecurityGroupImpactParameters
    | CrossAccountTrustParameters
    | DatabaseAccessParameters
    | InternetExposureParameters,
    Field(discriminator="kind"),
]
