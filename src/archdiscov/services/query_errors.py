from collections.abc import Mapping
from typing import ClassVar

from archdiscov.models.enums import QueryErrorCode

from .safe_errors import SafeServiceError


class QueryError(SafeServiceError):
    """Expose an expected query failure through a stable code and fixed message.

    Empty search results, source limitations, and truncation are not errors.
    Services must not translate unexpected programming defects into invalid requests.
    """

    code_type = QueryErrorCode
    messages: ClassVar[Mapping[QueryErrorCode, str]] = {
        QueryErrorCode.INVALID_REQUEST: "Invalid query request.",
        QueryErrorCode.RESOURCE_NOT_FOUND: "Resource was not found in the snapshot.",
    }
