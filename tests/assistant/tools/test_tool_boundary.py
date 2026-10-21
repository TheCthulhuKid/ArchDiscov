"""Cross-cutting checks over the whole tool set, derived from the registry.

A new tool fails these tests until it has a scenario below and its schema passes the audit.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from archdiscov.assistant.conversation import (
    MAX_TOOL_INPUT_SCHEMA_BYTES,
    Conversation,
    ParsedToolArguments,
    ToolCall,
    UserMessage,
)
from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.provider import CompletionRequest, RequestLimits, complete
from archdiscov.assistant.tools.catalog import ALL_TOOLS, default_registry
from archdiscov.assistant.tools.contracts import ID_FIELDS, IdKind, ToolErrorCode
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT

REGISTRY = default_registry()
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
OWNERSHIP_RESULT = FIXTURES_ROOT / "analysis" / "ownership.json"

# Words that would let a model choose data, reach a network, run commands, or change state.
FORBIDDEN_WORDS = {
    "path", "paths", "file", "files", "filename", "dir", "directory", "folder",
    "snapshot", "snapshots", "url", "uri", "host", "hostname", "endpoint", "address",
    "command", "cmd", "shell", "exec", "execute", "script", "code",
    "write", "save", "output", "delete", "remove", "upload", "download", "persist",
}  # fmt: skip
FORBIDDEN_FORMATS = {"uri", "uri-reference", "iri", "hostname", "path", "binary"}


def _session(name: str = "network.json", **kwargs: Any) -> ToolSession:
    return ToolSession.from_paths(SNAPSHOTS / name, **kwargs)


def _call(session: ToolSession, tool: str, **arguments: Any) -> tuple[bool, dict]:
    result = REGISTRY.dispatch(
        session, ToolCall(id="call_1", name=tool, arguments=ParsedToolArguments(value=arguments))
    )
    return result.is_error, json.loads(result.content)


# ---- schema audit ------------------------------------------------------------------


def _properties(schema: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Every property with its schema, through objects, arrays, and anyOf branches."""
    found = []
    for name, child in schema.get("properties", {}).items():
        found.append((prefix + name, child))
        found.extend(_properties(child, f"{prefix}{name}."))
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(key, []):
            found.extend(_properties(branch, prefix))
    if isinstance(schema.get("items"), dict):
        found.extend(_properties(schema["items"], prefix))
    return found


def _leaves(schema: dict) -> list[dict]:
    """Typed leaf schemas, expanding anyOf branches and array items."""
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        return [leaf for branch in branches for leaf in _leaves(branch)]
    if schema.get("type") == "array":
        return [schema, *_leaves(schema.get("items", {}))]
    return [schema]


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_schemas_have_no_write_network_shell_or_path_inputs(tool) -> None:
    schema = tool.declaration.input_schema
    assert schema["type"] == "object"
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    for name, child in _properties(schema):
        words = set(name.lower().replace(".", "_").split("_"))
        assert not words & FORBIDDEN_WORDS, (tool.name, name)
        for leaf in _leaves(child):
            assert leaf.get("format") not in FORBIDDEN_FORMATS, (tool.name, name)


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_every_input_is_bounded_and_closed(tool) -> None:
    schema = tool.declaration.input_schema
    objects = [schema] + [
        child for _, child in _properties(schema) if child.get("type") == "object"
    ]
    for item in objects:
        assert item.get("additionalProperties") is False, tool.name
    for name, child in _properties(schema):
        for leaf in _leaves(child):
            kind = leaf.get("type")
            if kind == "string":
                assert "enum" in leaf or "const" in leaf or "maxLength" in leaf, (tool.name, name)
            elif kind == "integer":
                assert "minimum" in leaf and "maximum" in leaf, (tool.name, name)
            elif kind == "array":
                assert "maxItems" in leaf, (tool.name, name)
            else:
                assert kind in {"null", "boolean", "object", None}, (tool.name, name, kind)


def test_every_declaration_is_valid_for_step_34_providers() -> None:
    declarations = REGISTRY.declarations()
    assert [item.name for item in declarations] == [tool.name for tool in ALL_TOOLS]
    for declaration in declarations:
        assert len(json.dumps(declaration.input_schema)) <= MAX_TOOL_INPUT_SCHEMA_BYTES
    request = CompletionRequest(
        conversation=Conversation(messages=(UserMessage(text="What is exposed?"),)),
        tools=declarations,
        limits=RequestLimits(max_tool_calls_per_turn=4),
    )
    transcript = FakeTranscript.model_validate(
        {
            "turns": [
                {
                    "kind": "respond",
                    "expect": {"tool_names": [item.name for item in declarations]},
                    "response": {
                        "message": {"role": "assistant", "text": "Done."},
                        "stop_reason": "end",
                    },
                }
            ]
        }
    )
    provider = FakeProvider(transcript)
    assert complete(provider, request).message.text == "Done."
    assert provider.requests[0].request.tools == declarations
    assert CompletionRequest.model_validate_json(request.model_dump_json()) == request


# ---- every tool: success, invalid input, unknown IDs, Azure ------------------------

VPC_ID = "123456789012/eu-west-1/ec2/physical/vpc-example"
UNKNOWN = "123456789012/eu-west-1/ec2/physical/missing"


def _first_resource(session: ToolSession) -> str:
    return session.index.resource_ids[0]


def _first_evidence(session: ToolSession) -> str:
    return session.index.evidence_ids[0]


# tool: (success arguments, invalid arguments, unknown-ID arguments or None)
SCENARIOS: dict[str, tuple[Any, dict, Any]] = {
    "search_resources": (lambda s: {}, {"limit": 0}, None),
    "inspect_resource": (
        lambda s: {"resource_id": _first_resource(s)},
        {},
        {"resource_id": UNKNOWN},
    ),
    "get_neighbours": (
        lambda s: {"resource_id": _first_resource(s), "direction": "both", "edges": "both"},
        {"resource_id": "x", "direction": "up"},
        {"resource_id": UNKNOWN},
    ),
    "find_paths": (
        lambda s: {"resource_id": _first_resource(s), "direction": "both", "edges": "both"},
        {"resource_id": "x", "depth": 0},
        {"resource_id": UNKNOWN},
    ),
    "inspect_coverage": (lambda s: {}, {"status": "unknown"}, None),
    "get_evidence": (lambda s: {"evidence_id": _first_evidence(s)}, {}, {"evidence_id": "missing"}),
    "list_analyzers": (lambda s: {}, {"kind": "ownership"}, None),
    "run_analyzer": (
        lambda s: {"kind": "ownership"},
        {"kind": "ownership", "limit": 101},
        {"kind": "security_group_impact", "resource_id": UNKNOWN},
    ),
    "list_saved_results": (lambda s: {}, {"number": 1}, None),
    "get_saved_analysis": (lambda s: {"number": 1}, {"number": 0}, {"number": 99}),
    "get_saved_comparison": (None, {"number": 0}, None),
}


def test_every_registered_tool_has_a_scenario() -> None:
    assert set(SCENARIOS) == set(REGISTRY.tools)


def _scenario_session(name: str) -> ToolSession:
    if name == "get_saved_analysis":
        return ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    return _session()


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_tool_succeeds_rejects_invalid_input_and_unknown_ids(name: str) -> None:
    success, invalid, unknown = SCENARIOS[name]
    session = _scenario_session(name)
    if success is not None:
        is_error, data = _call(session, name, **success(session))
        assert not is_error and data["status"] == "ok", (name, data)
    is_error, data = _call(session, name, **invalid)
    assert is_error and data["code"] == ToolErrorCode.INVALID_ARGUMENTS, (name, data)
    if unknown is not None:
        is_error, data = _call(session, name, **unknown)
        assert is_error and data["code"] == ToolErrorCode.NOT_FOUND, (name, data)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_tool_handles_azure_snapshots(name: str) -> None:
    session = _session("azure.json")
    success, _, _ = SCENARIOS[name]
    if name in {"get_saved_analysis", "get_saved_comparison"}:
        is_error, data = _call(session, name, number=1)
        assert is_error and data["code"] == ToolErrorCode.UNAVAILABLE
        return
    is_error, data = _call(session, name, **success(session))
    assert not is_error, (name, data)
    if name == "run_analyzer":
        for kind, extra in [
            ("security_group_impact", {"resource_id": _first_resource(session)}),
            ("internet_exposure", {}),
        ]:
            _, data = _call(session, name, kind=kind, **extra)
            codes = {
                limitation["code"]
                for finding in data["findings"]["items"]
                for limitation in finding["limitations"]
            }
            assert codes == {"unsupported_platform"}


# ---- returned-ID tracking across a run ---------------------------------------------


def _independent_ids(content: Any, snapshot_id: str | None = None) -> set[tuple[str, str, str]]:
    """IDs in delivered JSON, scoped to the nearest snapshot_id, without the record's code."""
    found: set[tuple[str, str, str]] = set()
    if isinstance(content, dict):
        snapshot_id = content.get("snapshot_id", snapshot_id)
        for key, value in content.items():
            if key in ID_FIELDS:
                if isinstance(value, dict) and "items" in value:
                    value = value["items"]
                for item in [value] if isinstance(value, str) else value or []:
                    found.add((snapshot_id, ID_FIELDS[key].value, item))
            else:
                found |= _independent_ids(value, snapshot_id)
    elif isinstance(content, list):
        for item in content:
            found |= _independent_ids(item, snapshot_id)
    return found


def _recorded(returned: ReturnedIds) -> set[tuple[str, str, str]]:
    return {
        (str(entry.snapshot_id), kind.value, value)
        for entry in returned.snapshots
        for kind in IdKind
        for value in getattr(entry, f"{kind.value}_ids")
    }


def _multi_tool_run(snapshot: Path, analysis: Path | None) -> tuple[ToolSession, list[dict]]:
    session = ToolSession.from_paths(
        snapshot, analysis_paths=[] if analysis is None else [analysis]
    )
    resource = session.index.resource_ids[0]
    calls = [
        ("search_resources", {}),
        ("inspect_resource", {"resource_id": resource}),
        ("get_neighbours", {"resource_id": resource, "direction": "both", "edges": "both"}),
        ("find_paths", {"resource_id": resource, "direction": "both", "edges": "both"}),
        ("inspect_coverage", {}),
        ("run_analyzer", {"kind": "ownership"}),
        ("inspect_resource", {"resource_id": UNKNOWN}),
        ("unknown_tool", {}),
        ("list_saved_results", {}),
    ]
    if session.index.evidence_ids:
        calls.append(("get_evidence", {"evidence_id": session.index.evidence_ids[-1]}))
    if analysis is not None:
        calls.append(("get_saved_analysis", {"number": 1}))
    delivered = []
    for name, arguments in calls:
        is_error, data = _call(session, name, **arguments)
        if not is_error:
            delivered.append(data)
    return session, delivered


@pytest.mark.parametrize(
    "snapshot,analysis",
    [
        (SNAPSHOTS / "network.json", None),
        (SNAPSHOTS / "dependency-targets.json", None),
        (SNAPSHOTS / "azure.json", None),
        (OWNERSHIP_SNAPSHOT, OWNERSHIP_RESULT),
    ],
    ids=["network", "terminals", "azure", "saved-analysis"],
)
def test_returned_id_tracking_is_exact_across_a_multi_tool_run(
    snapshot: Path, analysis: Path | None
) -> None:
    session, delivered = _multi_tool_run(snapshot, analysis)
    expected = set().union(*(_independent_ids(content) for content in delivered))
    assert expected
    assert _recorded(session.record.returned()) == expected
    assert {UUID(snapshot_id) for snapshot_id, _, _ in expected} == {session.index.snapshot_id}
    again, _ = _multi_tool_run(snapshot, analysis)
    assert again.record.returned() == session.record.returned()
