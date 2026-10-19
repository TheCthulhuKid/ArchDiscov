"""Safe diagnostics and detached parsing for analysis results."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.parameters import (
    CrossAccountTrustParameters,
    DatabaseAccessParameters,
    InternetExposureParameters,
    OwnershipParameters,
    SecurityGroupImpactParameters,
)
from archdiscov.models.analysis.references import (
    AnalysisLimitation,
    ContainmentPathStep,
    CoverageReference,
    DependencyPathStep,
    ResourceReference,
    UnresolvedTargetReference,
)
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import AwsTargetScope, CloudExternalTarget
from archdiscov.models.cloud.scope import AwsScope, AzureScope

from .validation_data import detach_data

_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (
        AnalysisResult,
        AnalysisFinding,
        AnalysisLimitation,
        ResourceReference,
        CoverageReference,
        DependencyPathStep,
        ContainmentPathStep,
        UnresolvedTargetReference,
        SecurityGroupImpact,
        CrossAccountTrust,
        LocalTarget,
        CloudExternalTarget,
        UnresolvedTarget,
        AwsScope,
        AzureScope,
        AwsTargetScope,
        OwnershipParameters,
        SecurityGroupImpactParameters,
        CrossAccountTrustParameters,
        DatabaseAccessParameters,
        InternetExposureParameters,
    )
    for field in model.model_fields
)


@dataclass(frozen=True)
class AnalysisValidationIssue:
    location: tuple[str | int, ...]
    type: str
    message: str


class AnalysisValidationError(Exception):
    """Safe diagnostics; raw input and exception contexts are not public details."""

    def __init__(
        self, details: tuple[AnalysisValidationIssue, ...], *, path: Path | None = None
    ) -> None:
        super().__init__("Analysis result validation failed.")
        self.code = "invalid_analysis_result"
        self.details = details
        self.path = path


def reject(location: tuple[str | int, ...], code: str, message: str) -> NoReturn:
    raise AnalysisValidationError((AnalysisValidationIssue(location, code, message),))


def parse_analysis_result(data: AnalysisResult | Mapping[str, Any]) -> AnalysisResult:
    """Revalidate detached current data without exposing untrusted diagnostic details."""
    try:
        return AnalysisResult.model_validate(detach_data(data))
    except ValidationError as exc:
        # Dynamic input keys and discriminator tags can occur in Pydantic locations.
        # Do not expose them, messages, inputs, or contexts from untrusted payloads.
        details = tuple(
            AnalysisValidationIssue(
                tuple(
                    part if isinstance(part, int) or part in _DIAGNOSTIC_FIELDS else "field"
                    for part in error["loc"]
                ),
                error["type"],
                "Invalid analysis result field.",
            )
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise AnalysisValidationError(details) from exc
    except (
        PydanticSerializationError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise AnalysisValidationError(
            (
                AnalysisValidationIssue(
                    (), "serialization_error", "Analysis result is not serializable."
                ),
            )
        ) from exc
