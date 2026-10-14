import math
from typing import Any

MAX_BYTES = 1_048_576
MAX_NODES = 50_000
MAX_DEPTH = 64


def validate_json(value: Any) -> None:
    """Bound expanded structures, including shared aliases, without recursive traversal."""
    stack = [(value, 0, frozenset())]
    nodes = 0
    size = 0
    while stack:
        item, depth, ancestors = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError
        if type(item) in (dict, list):
            if id(item) in ancestors:
                raise ValueError
            ancestors = ancestors | {id(item)}
            if len(item) > MAX_NODES:
                raise ValueError
            if nodes + len(stack) + len(item) > MAX_NODES:
                raise ValueError
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError
                    size += len(key.encode("utf-8"))
                    stack.append((child, depth + 1, ancestors))
            else:
                stack.extend((child, depth + 1, ancestors) for child in item)
        elif type(item) is str:
            size += len(item.encode("utf-8"))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise ValueError
        elif item is not None and type(item) is not bool:
            raise ValueError
        if size > MAX_BYTES:
            raise ValueError
