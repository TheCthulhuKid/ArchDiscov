from archdiscov.models.enums import QueryErrorCode

_MESSAGES = {
    QueryErrorCode.INVALID_REQUEST: "Invalid query request.",
    QueryErrorCode.RESOURCE_NOT_FOUND: "Resource was not found in the snapshot.",
}


class QueryError(Exception):
    """Expose an expected query failure through a stable code and fixed message.

    Messages contain no raw request values or validation exception text. Empty
    search results, source limitations, and truncation are not errors. Services
    must not translate unexpected programming defects into invalid requests.
    """

    def __init__(self, code: QueryErrorCode) -> None:
        self.code = QueryErrorCode(code)
        self.message = _MESSAGES[self.code]
        super().__init__(self.message)
