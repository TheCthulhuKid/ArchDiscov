"""Tool input models and the plain JSON schemas providers receive for them.

Kept apart from the registry so contracts that only declare inputs (such as the answer
proposal) do not import the tool session, graph index, or services.
"""

from typing import Any, ClassVar

from ..conversation import Contract

# Data is selected by the user for the session, never by the model.
_FORBIDDEN_INPUT_WORDS = frozenset(
    {"path", "paths", "file", "files", "filename", "dir", "directory", "snapshot", "snapshots"}
    | {"url", "uri"}
)


class ToolInput(Contract):
    """Base for tool inputs: unknown fields rejected; no path or snapshot selectors."""

    forbidden_words: ClassVar[frozenset[str]] = _FORBIDDEN_INPUT_WORDS

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name in cls.model_fields:
            if cls.forbidden_words & set(name.lower().split("_")):
                raise TypeError(f"{cls.__name__}.{name}: tools cannot select paths or snapshots")


class EmptyInput(ToolInput):
    """For tools that take no arguments."""


def inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local `$ref`s with their `$defs`, so every provider receives one plain schema.

    Tool inputs are not recursive, so inlining always terminates.
    """
    definitions = schema.get("$defs", {})

    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                siblings = {key: item for key, item in value.items() if key != "$ref"}
                return resolve({**definitions[reference.removeprefix("#/$defs/")], **siblings})
            return {key: resolve(item) for key, item in value.items() if key != "$defs"}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return resolve(schema)
