import json
from uuid import UUID

import pytest
from pydantic import Field, ValidationError

from archdiscov.assistant.conversation import (
    Contract,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    ToolCall,
)
from archdiscov.assistant.tools.contracts import (
    BoundedList,
    IdKind,
    ToolError,
    ToolErrorCode,
    ToolLimits,
    ToolOutput,
    UntrustedText,
    bounded,
    tool_error,
    untrusted,
)
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.registry import (
    EmptyInput,
    ToolDefinition,
    ToolInput,
    ToolRegistry,
    inline_schema,
)
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "aws.json"
SECRET = "SECRET_PRIVATE_TOKEN"


class _Listed(ToolOutput):
    snapshot_id: UUID
    resource_ids: BoundedList[str]


class _Named(ToolOutput):
    snapshot_id: UUID
    resource_id: str
    name: UntrustedText | None


class _Unscoped(ToolOutput):
    resource_id: str


class _Options(Contract):
    depth: int = Field(default=1, ge=1, le=3)


class _ResourceInput(ToolInput):
    resource_id: str
    options: _Options = _Options()


def _list(session: ToolSession, _: EmptyInput) -> ToolOutput:
    return _Listed(
        snapshot_id=session.index.snapshot_id,
        resource_ids=bounded(session.index.resource_ids, session.limits.result_limit),
    )


def _inspect(session: ToolSession, arguments: _ResourceInput) -> ToolOutput | ToolError:
    resource = session.index.get_resource(arguments.resource_id)
    if resource is None:
        return tool_error(ToolErrorCode.NOT_FOUND)
    return _Named(
        snapshot_id=session.index.snapshot_id,
        resource_id=resource.id,
        name=None if resource.name is None else untrusted(resource.name),
    )


def _explode(session: ToolSession, _: EmptyInput) -> ToolOutput:
    raise RuntimeError(f"connection string {SECRET}")


def _not_an_output(session: ToolSession, _: EmptyInput):
    return {"resource_id": SECRET}


def _unscoped(session: ToolSession, _: EmptyInput) -> ToolOutput:
    return _Unscoped(resource_id=session.index.resource_ids[0])


def _oversized(session: ToolSession, _: EmptyInput) -> ToolOutput:
    ids = list(session.index.resource_ids) * 50
    return _Listed(snapshot_id=session.index.snapshot_id, resource_ids=bounded(ids, len(ids)))


TOOLS = [
    ToolDefinition("list_resources", "List stable resource IDs.", EmptyInput, _list),
    ToolDefinition("inspect_resource", "Inspect one resource.", _ResourceInput, _inspect),
    ToolDefinition("explode", "Always fails.", EmptyInput, _explode),
    ToolDefinition("not_an_output", "Returns the wrong type.", EmptyInput, _not_an_output),
    ToolDefinition("unscoped", "Returns an ID without a snapshot.", EmptyInput, _unscoped),
    ToolDefinition("oversized", "Returns too much.", EmptyInput, _oversized),
]


@pytest.fixture
def session() -> ToolSession:
    return ToolSession.from_paths(SNAPSHOT, limits=ToolLimits(max_output_chars=4_096))


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(TOOLS)


def _call(name: str, arguments: dict | None = None, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=ParsedToolArguments(value=arguments or {}))


def _error(result) -> dict:
    assert result.is_error is True
    return json.loads(result.content)


# ---- declarations ------------------------------------------------------------------


def test_declarations_are_derived_from_input_models_in_order(registry: ToolRegistry) -> None:
    declarations = registry.declarations()
    assert [declaration.name for declaration in declarations] == [tool.name for tool in TOOLS]
    inspect = declarations[1]
    assert inspect.input_schema == inline_schema(_ResourceInput.model_json_schema())
    assert "$defs" in _ResourceInput.model_json_schema()
    assert inspect.input_schema["properties"]["options"]["properties"]["depth"]["maximum"] == 3
    assert "$ref" not in json.dumps(inspect.input_schema)
    assert inspect.input_schema["additionalProperties"] is False
    assert inspect.input_schema["required"] == ["resource_id"]
    assert declarations[0].input_schema == {
        "additionalProperties": False,
        "description": "For tools that take no arguments.",
        "properties": {},
        "title": "EmptyInput",
        "type": "object",
    }


def test_registry_rejects_duplicate_and_invalid_tools() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([TOOLS[0], TOOLS[0]])
    with pytest.raises(ValidationError):
        ToolRegistry([ToolDefinition("list-resources", "Bad name.", EmptyInput, _list)])
    with pytest.raises(ValidationError):
        ToolRegistry([ToolDefinition("blank", " ", EmptyInput, _list)])


def test_registry_is_a_detached_mapping(registry: ToolRegistry) -> None:
    tools = registry.tools
    tools.clear()
    assert len(registry.tools) == len(TOOLS)


@pytest.mark.parametrize(
    "field",
    ["path", "snapshot_id", "snapshot", "file_name", "output_file", "url", "base_uri", "dir"],
)
def test_inputs_cannot_declare_path_or_snapshot_selectors(field: str) -> None:
    with pytest.raises(TypeError, match="cannot select paths or snapshots"):
        type(f"Bad_{field}", (ToolInput,), {"__annotations__": {field: str}})


@pytest.mark.parametrize("field", ["resource_id", "security_group_id", "profile_name", "depth"])
def test_ordinary_field_names_are_allowed(field: str) -> None:
    model = type(f"Ok_{field}", (ToolInput,), {"__annotations__": {field: str}})
    assert field in model.model_fields


# ---- dispatch ----------------------------------------------------------------------


def test_successful_dispatch_returns_output_and_records_ids(
    session: ToolSession, registry: ToolRegistry
) -> None:
    result = registry.dispatch(session, _call("list_resources", call_id="call_list"))
    assert result.call_id == "call_list"
    assert result.is_error is False
    output = _Listed.model_validate_json(result.content)
    assert output.resource_ids.items == session.index.resource_ids[:25]
    returned = session.record.returned()
    for resource_id in output.resource_ids.items:
        assert returned.contains(IdKind.RESOURCE, session.index.snapshot_id, resource_id)


def test_inspect_returns_marked_names(session: ToolSession, registry: ToolRegistry) -> None:
    resource_id = next(
        rid for rid in session.index.resource_ids if session.index.get_resource(rid).name
    )
    result = registry.dispatch(session, _call("inspect_resource", {"resource_id": resource_id}))
    data = json.loads(result.content)
    assert data["resource_id"] == resource_id
    assert set(data["name"]) == {"untrusted_text", "truncated"}


def test_tool_errors_pass_through_and_record_nothing(
    session: ToolSession, registry: ToolRegistry
) -> None:
    result = registry.dispatch(session, _call("inspect_resource", {"resource_id": "missing"}))
    assert _error(result)["code"] == ToolErrorCode.NOT_FOUND
    assert session.record.returned() == ReturnedIds()


def test_unknown_tools_are_safe_errors(session: ToolSession, registry: ToolRegistry) -> None:
    result = registry.dispatch(session, _call("delete_everything"))
    assert _error(result) == json.loads(tool_error(ToolErrorCode.UNKNOWN_TOOL).model_dump_json())


@pytest.mark.parametrize("reason", list(MalformedArgumentsReason))
def test_malformed_arguments_are_safe_errors(
    session: ToolSession, registry: ToolRegistry, reason: MalformedArgumentsReason
) -> None:
    call = ToolCall(
        id="call_1", name="inspect_resource", arguments=MalformedToolArguments(reason=reason)
    )
    assert _error(registry.dispatch(session, call))["code"] == ToolErrorCode.MALFORMED_ARGUMENTS


@pytest.mark.parametrize(
    "arguments,locations",
    [
        ({}, ["resource_id"]),
        ({"resource_id": 5}, ["resource_id"]),
        ({"resource_id": "vpc", "options": {"depth": 9}}, ["options.depth"]),
        ({"resource_id": "vpc", "options": {"depth": "deep"}}, ["options.depth"]),
        ({"resource_id": "vpc", "snapshot": "/etc/passwd"}, ["field"]),
        ({"resource_id": "vpc", "path": "../other.json"}, ["field"]),
        ({"resource_id": "vpc", SECRET: SECRET}, ["field"]),
        ({"resource_id": "vpc", "options": {"file": "x"}}, ["options.field"]),
    ],
)
def test_invalid_arguments_name_only_declared_fields(
    session: ToolSession, registry: ToolRegistry, arguments: dict, locations: list[str]
) -> None:
    error = _error(registry.dispatch(session, _call("inspect_resource", arguments)))
    assert error["code"] == ToolErrorCode.INVALID_ARGUMENTS
    assert error["locations"] == locations
    assert SECRET not in json.dumps(error)
    assert "passwd" not in json.dumps(error)


def test_arguments_for_empty_inputs_are_rejected(
    session: ToolSession, registry: ToolRegistry
) -> None:
    error = _error(registry.dispatch(session, _call("list_resources", {"snapshot_id": "x"})))
    assert error["code"] == ToolErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize("name", ["explode", "not_an_output", "unscoped"])
def test_unexpected_failures_are_internal_errors_without_details(
    session: ToolSession, registry: ToolRegistry, name: str
) -> None:
    result = registry.dispatch(session, _call(name))
    assert _error(result)["code"] == ToolErrorCode.INTERNAL_ERROR
    assert SECRET not in result.content
    assert session.record.returned() == ReturnedIds()


def test_oversized_outputs_are_replaced_and_not_recorded(
    session: ToolSession, registry: ToolRegistry
) -> None:
    result = registry.dispatch(session, _call("oversized"))
    assert _error(result)["code"] == ToolErrorCode.OUTPUT_TOO_LARGE
    assert len(result.content) <= session.limits.max_output_chars
    assert session.record.returned() == ReturnedIds()


def test_dispatch_is_deterministic(registry: ToolRegistry) -> None:
    results = []
    for _ in range(2):
        session = ToolSession.from_paths(SNAPSHOT)
        results.append(
            (
                registry.dispatch(session, _call("list_resources")),
                session.record.returned(),
            )
        )
    assert results[0] == results[1]
