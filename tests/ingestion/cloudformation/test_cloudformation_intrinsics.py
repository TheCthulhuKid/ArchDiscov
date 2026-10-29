import pytest

from archdiscov.ingestion.cloudformation import intrinsics
from archdiscov.ingestion.cloudformation.intrinsics import (
    IntrinsicLimitExceeded,
    InvalidIntrinsic,
    evaluate_pure,
)


def test_pure_operations_compare_by_type_and_reject_invalid_operands():
    assert evaluate_pure("Fn::Equals", [1, True]) is False
    assert evaluate_pure("Fn::Split", [",", "a,,b"]) == ["a", "", "b"]
    for name, values in (("Fn::Select", [-1, ["a"]]), ("Fn::FindInMap", ["M", "a", "b"])):
        with pytest.raises(InvalidIntrinsic):
            evaluate_pure(name, values)


def test_split_node_limit(monkeypatch):
    monkeypatch.setattr(intrinsics, "MAX_NODES", 3)
    with pytest.raises(IntrinsicLimitExceeded):
        evaluate_pure("Fn::Split", [",", "a,b,c,d"])
