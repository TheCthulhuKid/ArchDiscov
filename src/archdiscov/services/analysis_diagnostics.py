"""Safe diagnostics and detached parsing for analysis results."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.models.analysis.database_access import AccessElementDecision, DatabaseAccess
from archdiscov.models.analysis.exposure import ExposureElementDecision, InternetExposure
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
from archdiscov.models.validation_errors import ValidationIssue as AnalysisValidationIssue
from archdiscov.models.validation_errors import safe_validation_issues, serialization_issue

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
        DatabaseAccess,
        AccessElementDecision,
        InternetExposure,
        ExposureElementDecision,
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


class AnalysisValidationError(Exception):
    """Safe diagnostics; raw input and exception contexts are not public details.

    Result kinds with the same diagnostics (comparisons) subclass this and name their subject.
    """

    subject = "Analysis result"
    code = "invalid_analysis_result"

    def __init__(
        self, details: tuple[AnalysisValidationIssue, ...], *, path: Path | None = None
    ) -> None:
        super().__init__(f"{self.subject} validation failed.")
        self.details = details
        self.path = path


def reject(location: tuple[str | int, ...], code: str, message: str) -> NoReturn:
    raise AnalysisValidationError((AnalysisValidationIssue(location, code, message),))


def parse_analysis_result(data: AnalysisResult | Mapping[str, Any]) -> AnalysisResult:
    """Revalidate detached current data without exposing untrusted diagnostic details."""
    try:
        return AnalysisResult.model_validate(detach_data(data))
    except ValidationError as exc:
        raise AnalysisValidationError(
            safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, "analysis result")
        ) from exc
    except (
        PydanticSerializationError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise AnalysisValidationError((serialization_issue("Analysis result"),)) from exc
