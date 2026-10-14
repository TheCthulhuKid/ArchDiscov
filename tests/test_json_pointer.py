import pytest

from archdiscov.models.json_pointer import escape_pointer_token


@pytest.mark.parametrize(
    "token,expected",
    [("", ""), ("plain", "plain"), ("a/b~c", "a~1b~0c"), ("~1/~0", "~01~1~00"), (0, "0")],
)
def test_escape_pointer_token(token, expected):
    assert escape_pointer_token(token) == expected
