from archdiscov.models.json_pointer import escape_pointer_token, read_pointer


def test_escape_pointer_token():
    assert escape_pointer_token("a/b~c") == "a~1b~0c"
    assert escape_pointer_token(0) == "0"


def test_read_trusted_pointer():
    assert read_pointer({"a/b~c": [None, {"": "value"}]}, "/a~1b~0c/1/") == "value"
