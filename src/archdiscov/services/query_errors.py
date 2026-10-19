from archdiscov.models.enums import QueryErrorCode

from .safe_errors import SafeServiceError


class QueryError(SafeServiceError):
    """Expose an expected query failure through a stable code and fixed message.

    Empty search results, source limitations, and truncation are not errors.
    Services must not translate unexpected programming defects into invalid requests.
    """

    code_type = QueryErrorCode
    messages = {
        QueryErrorCode.INVALID_REQUEST: "Invalid query request.",
        QueryErrorCode.RESOURCE_NOT_FOUND: "Resource was not found in the snapshot.",
    }
