import pytest

from archdiscov.models.enums import QueryErrorCode
from archdiscov.services.query_errors import QueryError


@pytest.mark.parametrize(
    "code,message",
    [
        (QueryErrorCode.INVALID_REQUEST, "Invalid query request."),
        (
            QueryErrorCode.RESOURCE_NOT_FOUND,
            "Resource was not found in the snapshot.",
        ),
    ],
)
def test_query_error_preserves_code_and_safe_message(code, message):
    error = QueryError(code)

    assert error.code is code
    assert error.message == message
    assert str(error) == message
    assert error.args == (message,)
