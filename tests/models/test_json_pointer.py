import pytest

from archdiscov.models.json_pointer import escape_pointer_token, read_pointer


@pytest.mark.parametrize(
    "token,expected",
    [("", ""), ("plain", "plain"), ("a/b~c", "a~1b~0c"), ("~1/~0", "~01~1~00"), (0, "0")],
)
def test_escape_pointer_token(token, expected):
    assert escape_pointer_token(token) == expected


@pytest.mark.parametrize(
    "pointer,expected",
    [("", {"a/b~c": [None, {"": "value"}]}), ("/a~1b~0c/0", None), ("/a~1b~0c/1/", "value")],
)
def test_read_trusted_pointer(pointer, expected):
    assert read_pointer({"a/b~c": [None, {"": "value"}]}, pointer) == expected
