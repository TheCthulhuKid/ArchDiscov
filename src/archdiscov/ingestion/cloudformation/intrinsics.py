import re
from typing import Any

from .evaluation_limits import MAX_BYTES, MAX_NODES

INTRINSIC_ARITIES = {
    "Fn::Join": (2, 2),
    "Fn::Split": (2, 2),
    "Fn::Select": (2, 2),
    "Fn::FindInMap": (3, 3),
    "Fn::Equals": (2, 2),
    "Fn::Not": (1, 1),
    "Fn::And": (2, 10),
    "Fn::Or": (2, 10),
}


class InvalidIntrinsic(ValueError):
    pass


class IntrinsicLimitExceeded(Exception):
    pass


def _join(values: list) -> str:
    delimiter, items = values
    if not (
        isinstance(delimiter, str)
        and isinstance(items, list)
        and all(isinstance(v, str) for v in items)
    ):
        raise InvalidIntrinsic
    size = sum(len(v.encode("utf-8")) for v in items)
    size += len(delimiter.encode("utf-8")) * max(0, len(items) - 1)
    if size > MAX_BYTES:
        raise IntrinsicLimitExceeded
    return delimiter.join(items)


def _split(values: list) -> list[str]:
    delimiter, source = values
    if not (isinstance(delimiter, str) and delimiter and isinstance(source, str)):
        raise InvalidIntrinsic
    if source.count(delimiter) >= MAX_NODES:
        raise IntrinsicLimitExceeded
    return source.split(delimiter)


def _select(values: list) -> Any:
    index, items = values
    if isinstance(index, str) and re.fullmatch(r"[0-9]{1,9}", index):
        index = int(index)
    if not (type(index) is int and isinstance(items, list) and 0 <= index < len(items)):
        raise InvalidIntrinsic
    return items[index]


def _equals(values: list) -> bool:
    if not all(type(v) in (str, int, float, bool, type(None)) for v in values):
        raise InvalidIntrinsic
    return type(values[0]) is type(values[1]) and values[0] == values[1]


_PURE_OPERATIONS = {
    "Fn::Join": _join,
    "Fn::Split": _split,
    "Fn::Select": _select,
    "Fn::Equals": _equals,
}
_BOOLEAN_OPERATIONS = {
    "Fn::And": all,
    "Fn::Or": any,
    "Fn::Not": lambda values: not values[0],
}


def evaluate_pure(name: str, values: list) -> Any:
    """Apply an operation to arity-checked, evaluated operands without evaluator state."""
    boolean_operation = _BOOLEAN_OPERATIONS.get(name)
    if boolean_operation is not None:
        if not all(type(v) is bool for v in values):
            raise InvalidIntrinsic
        return boolean_operation(values)
    operation = _PURE_OPERATIONS.get(name)
    if operation is None:
        raise InvalidIntrinsic
    return operation(values)
