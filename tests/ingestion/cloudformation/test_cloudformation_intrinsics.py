import pytest

from archdiscov.ingestion.cloudformation import intrinsics
from archdiscov.ingestion.cloudformation.intrinsics import (
    IntrinsicLimitExceeded,
    InvalidIntrinsic,
    evaluate_pure,
)


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    [
        ("Fn::Join", ["-", ["a", "b"]], "a-b"),
        ("Fn::Join", ["-", []], ""),
        ("Fn::Split", [",", "a,,b"], ["a", "", "b"]),
        ("Fn::Select", ["01", ["a", None]], None),
        ("Fn::Equals", [1, True], False),
        ("Fn::Equals", [1, 1.0], False),
        ("Fn::Equals", [None, None], True),
        ("Fn::And", [True, False], False),
        ("Fn::Or", [False, True], True),
        ("Fn::Not", [False], True),
    ],
)
def test_pure_operations(name, values, expected):
    assert evaluate_pure(name, values) == expected


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("Fn::Join", [1, ["a"]]),
        ("Fn::Split", ["", "a"]),
        ("Fn::Select", [-1, ["a"]]),
        ("Fn::And", [True, 1]),
        ("Fn::FindInMap", ["M", "a", "b"]),
        ("Fn::Unsupported", [True]),
    ],
)
def test_invalid_operands_or_non_pure_operations(name, values):
    with pytest.raises(InvalidIntrinsic):
        evaluate_pure(name, values)


def test_join_byte_limit_includes_utf8_and_delimiters(monkeypatch):
    monkeypatch.setattr(intrinsics, "MAX_BYTES", 5)
    assert evaluate_pure("Fn::Join", ["-", ["é", "é"]]) == "é-é"
    with pytest.raises(IntrinsicLimitExceeded):
        evaluate_pure("Fn::Join", ["--", ["é", "é"]])


def test_split_node_limit(monkeypatch):
    monkeypatch.setattr(intrinsics, "MAX_NODES", 3)
    assert evaluate_pure("Fn::Split", [",", "a,b,c"]) == ["a", "b", "c"]
    with pytest.raises(IntrinsicLimitExceeded):
        evaluate_pure("Fn::Split", [",", "a,b,c,d"])
