"""Tool definitions and dispatch from a `ToolCall` to a `ToolResult`.

A tool is a typed input model (its JSON schema, with references inlined, is the
declaration the provider sees), a function over the session returning a typed output or a
`ToolError`, and a name. The registry is a plain mapping. Dispatch never raises for model
mistakes or tool failures: unknown names, malformed or invalid arguments, and unexpected
exceptions become safe tool errors, and only delivered outputs are recorded.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from archdiscov.models.validation_errors import safe_validation_issues

from ..conversation import (
    Contract,
    MalformedToolArguments,
    ToolCall,
    ToolDeclaration,
    ToolResult,
)
from .contracts import ToolError, ToolErrorCode, ToolOutput, render, tool_error
from .inputs import EmptyInput, ToolInput, inline_schema
from .session import ToolSession

__all__ = [
    "EmptyInput",
    "ToolDefinition",
    "ToolInput",
    "ToolRegistry",
    "inline_schema",
]


@dataclass(frozen=True)
class ToolDefinition[I: ToolInput]:
    name: str
    description: str
    input_model: type[I]
    run: Callable[[ToolSession, I], ToolOutput | ToolError]

    @property
    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name,
            description=self.description,
            input_schema=inline_schema(self.input_model.model_json_schema()),
        )


class ToolRegistry:
    """Tools by name, declared in registration order."""

    def __init__(self, tools: Iterable[ToolDefinition]) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            tool.declaration  # noqa: B018 - validate the name, description, and schema now
            self._tools[tool.name] = tool

    @property
    def tools(self) -> Mapping[str, ToolDefinition]:
        return dict(self._tools)

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return tuple(tool.declaration for tool in self._tools.values())

    def dispatch(self, session: ToolSession, call: ToolCall) -> ToolResult:
        output = self._run(session, call)
        result, delivered = render(call.id, output, session.limits)
        if isinstance(delivered, ToolOutput):
            try:
                session.record.record_output(delivered)
            except Exception:  # noqa: BLE001 - an unrecordable output is an internal error
                result, _ = render(
                    call.id, tool_error(ToolErrorCode.INTERNAL_ERROR), session.limits
                )
        return result

    def _run(self, session: ToolSession, call: ToolCall) -> ToolOutput | ToolError:
        tool = self._tools.get(call.name)
        if tool is None:
            return tool_error(ToolErrorCode.UNKNOWN_TOOL)
        if isinstance(call.arguments, MalformedToolArguments):
            return tool_error(ToolErrorCode.MALFORMED_ARGUMENTS)
        try:
            arguments = tool.input_model.model_validate(call.arguments.value)
        except ValidationError as exc:
            fields = frozenset(_input_field_names(tool.input_model))
            issues = safe_validation_issues(exc, fields, "tool input")
            locations = [
                ".".join(str(part) for part in issue.location) for issue in issues if issue.location
            ]
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, sorted(set(locations))[:20])
        try:
            output = tool.run(session, arguments)
        except Exception:  # noqa: BLE001 - a failing tool is an internal error, not a crash
            return tool_error(ToolErrorCode.INTERNAL_ERROR)
        if not isinstance(output, (ToolOutput, ToolError)):
            return tool_error(ToolErrorCode.INTERNAL_ERROR)
        return output


def _input_field_names(model: type[ToolInput]) -> set[str]:
    """Declared field names of an input model and of models nested in it."""
    names: set[str] = set()
    pending = [model]
    while pending:
        current = pending.pop()
        for name, field in current.model_fields.items():
            names.add(name)
            annotation = field.annotation
            pending.extend(
                candidate
                for candidate in (annotation, *getattr(annotation, "__args__", ()))
                if isinstance(candidate, type) and issubclass(candidate, Contract)
            )
    return names
