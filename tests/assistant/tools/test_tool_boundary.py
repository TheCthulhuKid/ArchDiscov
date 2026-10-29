"""Cross-cutting checks over the whole tool set, derived from the registry.

A new tool fails these tests until it has a scenario below and its schema passes the audit.
"""

import json
from typing import Any
from uuid import UUID

import pytest

from archdiscov.assistant.conversation import (
    MAX_TOOL_INPUT_SCHEMA_BYTES,
    ParsedToolArguments,
    ToolCall,
)
from archdiscov.assistant.tools.catalog import ALL_TOOLS, default_registry
from archdiscov.assistant.tools.contracts import ID_FIELDS, IdKind, ToolErrorCode
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT
from tests.support.tool_audit import (
    assert_bounded_and_closed,
    assert_no_write_network_shell_or_path_inputs,
)

REGISTRY = default_registry()
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
OWNERSHIP_RESULT = FIXTURES_ROOT / "analysis" / "ownership.json"


def _session(name: str = "network.json") -> ToolSession:
    return ToolSession.from_paths(SNAPSHOTS / name)


def _call(session: ToolSession, tool: str, **arguments: Any) -> tuple[bool, dict]:
    result = REGISTRY.dispatch(
        session, ToolCall(id="call_1", name=tool, arguments=ParsedToolArguments(value=arguments))
    )
    return result.is_error, json.loads(result.content)


# ---- schema audit ------------------------------------------------------------------


def test_every_schema_is_bounded_closed_and_free_of_write_network_shell_or_path_inputs() -> None:
    for tool in ALL_TOOLS:
        assert_no_write_network_shell_or_path_inputs(tool)
        assert_bounded_and_closed(tool)


def test_every_declaration_is_valid_for_step_34_providers() -> None:
    declarations = REGISTRY.declarations()
    assert [item.name for item in declarations] == [tool.name for tool in ALL_TOOLS]
    for declaration in declarations:
        assert len(json.dumps(declaration.input_schema)) <= MAX_TOOL_INPUT_SCHEMA_BYTES


# ---- every tool: success, invalid input, unknown IDs, Azure ------------------------

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
    # An AWS-only analyzer's unsupported result on Azure is its own home's to prove.
    is_error, data = _call(session, name, **success(session))
    assert not is_error, (name, data)


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


def _multi_tool_run() -> tuple[ToolSession, list[dict]]:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
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
        ("get_evidence", {"evidence_id": session.index.evidence_ids[-1]}),
        ("get_saved_analysis", {"number": 1}),
    ]
    delivered = []
    for name, arguments in calls:
        is_error, data = _call(session, name, **arguments)
        if not is_error:
            delivered.append(data)
    return session, delivered


def test_returned_id_tracking_is_exact_across_a_multi_tool_run() -> None:
    session, delivered = _multi_tool_run()
    expected = set().union(*(_independent_ids(content) for content in delivered))
    assert expected
    assert _recorded(session.record.returned()) == expected
    assert {UUID(snapshot_id) for snapshot_id, _, _ in expected} == {session.index.snapshot_id}
