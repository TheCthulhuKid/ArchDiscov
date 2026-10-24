from collections.abc import Mapping
from typing import ClassVar

from archdiscov.models.enums import AnalyzerErrorCode

from .safe_errors import SafeServiceError


class AnalyzerError(SafeServiceError):
    """Expose an expected analyzer-boundary failure by stable code and fixed message.

    Findings, limitations, and insufficient evidence are results, not errors.
    Unexpected defects in an analyzer are not translated into these codes.
    """

    code_type = AnalyzerErrorCode
    messages: ClassVar[Mapping[AnalyzerErrorCode, str]] = {
        AnalyzerErrorCode.UNSUPPORTED_ANALYZER: "Analyzer is not available.",
        AnalyzerErrorCode.INVALID_PARAMETERS: "Invalid analyzer parameters.",
        AnalyzerErrorCode.INVALID_RESULT: "Analyzer returned an invalid result.",
        AnalyzerErrorCode.RESOURCE_NOT_FOUND: "Selected resource was not found in the snapshot.",
        AnalyzerErrorCode.UNSUPPORTED_RESOURCE: (
            "Selected resource is not supported by this analyzer."
        ),
    }
