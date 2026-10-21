import json
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.conversation import ParsedToolArguments, ToolCall
from archdiscov.assistant.tools.analysis_tools import ANALYSIS_TOOLS, PARAMETER_MODELS
from archdiscov.assistant.tools.contracts import ToolErrorCode, ToolLimits
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession, ToolSessionError
from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import publication
from archdiscov.services.analyses import load_analysis
from archdiscov.services.analysis_execution import DEFAULT_REGISTRY, run_analyzer
from archdiscov.services.analysis_validation import AnalysisValidationError
from archdiscov.services.comparison import compare_snapshots
from archdiscov.services.comparison_results import ComparisonValidationError, save_comparison
from archdiscov.services.snapshots import load_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.audit_scenarios import SCENARIOS, with_coverage

REGISTRY = ToolRegistry(ANALYSIS_TOOLS)
SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
OWNERSHIP_RESULT = FIXTURES_ROOT / "analysis" / "ownership.json"


def _run(session: ToolSession, tool: str, **arguments: Any) -> dict:
    result = REGISTRY.dispatch(
        session, ToolCall(id="call_1", name=tool, arguments=ParsedToolArguments(value=arguments))
    )
    data = json.loads(result.content)
    assert result.is_error is (data["status"] == "error")
    return data


def _service_result(snapshot, kind: AnalyzerKind, parameters: dict, depth: int) -> AnalysisResult:
    full = {"kind": kind, **parameters}
    if "result_limit" in PARAMETER_MODELS[kind].model_fields:
        full["result_limit"] = 10_000
    if "max_depth" in PARAMETER_MODELS[kind].model_fields:
        full["max_depth"] = min(full.get("max_depth", 3), depth)
    return run_analyzer(GraphIndex(snapshot), full)


def _expected_ids(result: AnalysisResult, findings: int) -> ReturnedIds:
    """Every finding, evidence, and resource ID the first `findings` findings cite."""
    finding_ids, evidence_ids, resource_ids = set(), set(), set()
    for finding in result.findings[:findings]:
        finding_ids.add(finding.id)
        evidence_ids.update(finding.evidence_ids)
        resource_ids.update(ref.resource_id for ref in finding.resources)
        for step in finding.path:
            evidence_ids.update(step.evidence_ids)
            resource_ids.add(step.source_id)
            if step.kind == "containment":
                resource_ids.add(step.target_id)
            elif isinstance(step.target, LocalTarget):
                resource_ids.add(step.target.resource_id)
        for limitation in finding.limitations:
            evidence_ids.update(limitation.evidence_ids)
            if limitation.unresolved_target is not None:
                evidence_ids.update(limitation.unresolved_target.evidence_ids)
                resource_ids.add(limitation.unresolved_target.source_id)
    if not (finding_ids or evidence_ids or resource_ids):
        return ReturnedIds()
    return ReturnedIds.model_validate(
        {
            "snapshots": [
                {
                    "snapshot_id": str(result.snapshot_id),
                    "finding_ids": sorted(finding_ids),
                    "evidence_ids": sorted(evidence_ids),
                    "resource_ids": sorted(resource_ids),
                }
            ]
        }
    )


# ---- list --------------------------------------------------------------------------


def test_list_analyzers_describes_every_kind_and_its_parameters() -> None:
    session = ToolSession(load_snapshot(SNAPSHOTS / "aws.json"), limits=ToolLimits(depth=5))
    data = _run(session, "list_analyzers")
    assert [item["kind"] for item in data["analyzers"]] == [kind.value for kind in AnalyzerKind]
    assert data["source_kind"] == session.index.source_kind.value
    assert data["collection_started_at"] and data["collection_completed_at"]
    for item in data["analyzers"]:
        kind = AnalyzerKind(item["kind"])
        assert item["version"] == DEFAULT_REGISTRY.get(kind).version
        fields = {
            name
            for name in PARAMETER_MODELS[kind].model_fields
            if name not in {"kind", "result_limit"}
        }
        assert {parameter["name"] for parameter in item["parameters"]} == fields
        for parameter in item["parameters"]:
            if parameter["name"] == "max_depth":
                assert (parameter["minimum"], parameter["maximum"], parameter["default"]) == (
                    1,
                    5,
                    3,
                )
            if parameter["name"] == "port":
                assert (parameter["minimum"], parameter["maximum"], parameter["required"]) == (
                    1,
                    65535,
                    True,
                )


# ---- run ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(AnalyzerKind))
def test_each_analyzer_runs_with_exact_results_and_recorded_ids(kind: AnalyzerKind) -> None:
    scenario = SCENARIOS[kind]
    snapshot = scenario.build()
    session = ToolSession(snapshot, limits=ToolLimits(result_limit=100))
    data = _run(session, "run_analyzer", kind=kind.value, limit=100, **scenario.parameters)
    expected = _service_result(snapshot, kind, scenario.parameters, session.limits.depth)
    assert data["saved"] is False
    assert data["analyzer"] == kind.value
    assert data["analyzer_version"] == expected.analyzer_version
    assert data["parameters"] == expected.parameters.model_dump(mode="json")
    findings = data["findings"]["items"]
    assert [item["finding_id"] for item in findings] == [item.id for item in expected.findings]
    for view, finding in zip(findings, expected.findings, strict=True):
        assert view["status"] == finding.status.value
        assert view["evidence_ids"] == finding.evidence_ids
        assert view["summary"]["untrusted_text"] == finding.summary[:200]
        assert [step["evidence_ids"] for step in view["path"]] == [
            step.evidence_ids for step in finding.path
        ]
        assert [item["code"] for item in view["limitations"]] == [
            item.code for item in finding.limitations
        ]
        for block in ("impact", "trust", "database_access", "internet_exposure"):
            value = getattr(finding, block)
            assert view[block] == (None if value is None else value.model_dump(mode="json"))
    assert session.record.returned() == _expected_ids(expected, len(findings))


def test_analysis_truncation_reports_the_dropped_findings() -> None:
    scenario = SCENARIOS[AnalyzerKind.CROSS_ACCOUNT_TRUST]
    snapshot = scenario.build()
    session = ToolSession(snapshot)
    total = len(_service_result(snapshot, scenario.kind, {}, 3).findings)
    assert total > 1
    data = _run(session, "run_analyzer", kind="cross_account_trust", limit=1)
    assert len(data["findings"]["items"]) == 1
    assert data["findings"]["truncation"] == {
        "reasons": ["result_limit"],
        "returned": 1,
        "available": total,
    }
    assert session.record.returned() == _expected_ids(
        _service_result(snapshot, scenario.kind, {}, 3), 1
    )


@pytest.mark.parametrize("kind", list(AnalyzerKind))
@pytest.mark.parametrize("status", ["failed", "incomplete", None])
def test_partial_coverage_is_qualified(kind: AnalyzerKind, status: str | None) -> None:
    scenario = SCENARIOS[kind]
    snapshot = with_coverage(scenario.build(), status)
    session = ToolSession(snapshot, limits=ToolLimits(result_limit=100))
    data = _run(session, "run_analyzer", kind=kind.value, limit=100, **scenario.parameters)
    expected = _service_result(snapshot, kind, scenario.parameters, 3)
    assert [
        (item["status"], [limitation["code"] for limitation in item["limitations"]])
        for item in data["findings"]["items"]
    ] == [
        (finding.status.value, [limitation.code for limitation in finding.limitations])
        for finding in expected.findings
    ]


@pytest.mark.parametrize(
    "kind,arguments,code,locations",
    [
        ("ownership", {"port": 5432}, ToolErrorCode.INVALID_ARGUMENTS, ["port"]),
        ("security_group_impact", {}, ToolErrorCode.INVALID_ARGUMENTS, ["resource_id"]),
        ("database_access", {"resource_id": "x"}, ToolErrorCode.INVALID_ARGUMENTS, ["port"]),
        (
            "internet_exposure",
            {"resource_id": "x"},
            ToolErrorCode.INVALID_ARGUMENTS,
            ["resource_id"],
        ),
        (
            "database_access",
            {"resource_id": "x", "port": 0},
            ToolErrorCode.INVALID_ARGUMENTS,
            ["port"],
        ),
        (
            "database_access",
            {"resource_id": "x", "port": 65536},
            ToolErrorCode.INVALID_ARGUMENTS,
            ["port"],
        ),
        ("internet_exposure", {"max_depth": 11}, ToolErrorCode.INVALID_ARGUMENTS, ["max_depth"]),
        ("internet_exposure", {"max_depth": 0}, ToolErrorCode.INVALID_ARGUMENTS, ["max_depth"]),
        ("ownership", {"ownership_keys": []}, ToolErrorCode.INVALID_ARGUMENTS, ["ownership_keys"]),
        ("ownership", {"ownership_keys": ["Owner", "Owner"]}, ToolErrorCode.INVALID_ARGUMENTS, []),
        ("drift", {}, ToolErrorCode.INVALID_ARGUMENTS, ["kind"]),
        ("ownership", {"result_limit": 5}, ToolErrorCode.INVALID_ARGUMENTS, ["field"]),
    ],
)
def test_invalid_parameters_are_rejected(
    kind: str, arguments: dict, code: ToolErrorCode, locations: list[str]
) -> None:
    session = ToolSession(SCENARIOS[AnalyzerKind.OWNERSHIP].build())
    data = _run(session, "run_analyzer", kind=kind, **arguments)
    assert data["code"] == code
    assert data["locations"] == locations
    assert session.record.returned() == ReturnedIds()


def test_depth_is_capped_by_the_session() -> None:
    scenario = SCENARIOS[AnalyzerKind.SECURITY_GROUP_IMPACT]
    session = ToolSession(scenario.build(), limits=ToolLimits(depth=2))
    data = _run(
        session,
        "run_analyzer",
        kind="security_group_impact",
        resource_id=scenario.parameters["resource_id"],
        max_depth=10,
    )
    assert data["parameters"]["max_depth"] == 2


def test_unknown_and_unsupported_resources() -> None:
    scenario = SCENARIOS[AnalyzerKind.SECURITY_GROUP_IMPACT]
    session = ToolSession(scenario.build())
    missing = _run(
        session,
        "run_analyzer",
        kind="security_group_impact",
        resource_id="123456789012/eu-west-1/ec2/physical/sg-missing",
    )
    assert missing["code"] == ToolErrorCode.NOT_FOUND
    not_a_group = next(
        rid for rid in session.index.resource_ids if "/sg-" not in rid and "security" not in rid
    )
    unsupported = _run(
        session, "run_analyzer", kind="security_group_impact", resource_id=not_a_group
    )
    assert unsupported["code"] == ToolErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    "kind,arguments",
    [
        ("security_group_impact", {"resource_id": "any"}),
        ("cross_account_trust", {}),
        ("database_access", {"resource_id": "any", "port": 5432}),
        ("internet_exposure", {}),
    ],
)
def test_azure_semantic_analyses_return_the_unsupported_result(kind: str, arguments: dict) -> None:
    session = ToolSession(load_snapshot(SNAPSHOTS / "azure.json"))
    resource_id = session.index.resource_ids[0]
    if "resource_id" in arguments:
        arguments = {**arguments, "resource_id": resource_id}
    data = _run(session, "run_analyzer", kind=kind, **arguments)
    findings = data["findings"]["items"]
    assert findings
    assert all(item["status"] == "insufficient_evidence" for item in findings)
    assert {item["code"] for finding in findings for item in finding["limitations"]} == {
        "unsupported_platform"
    }


def test_no_tool_writes_files(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("tools must not write files")

    monkeypatch.setattr(publication, "publish_text", forbidden)
    for kind, scenario in SCENARIOS.items():
        session = ToolSession(scenario.build())
        assert (
            _run(session, "run_analyzer", kind=kind.value, **scenario.parameters)["status"] == "ok"
        )
        assert _run(session, "list_analyzers")["status"] == "ok"


# ---- saved analyses ----------------------------------------------------------------


def test_saved_analyses_load_only_when_selected_and_verified() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    saved = load_analysis(OWNERSHIP_RESULT, snapshot=load_snapshot(OWNERSHIP_SNAPSHOT))
    listing = _run(session, "list_saved_results")
    assert listing["analyses"] == [
        {
            "number": 1,
            "snapshot_id": str(saved.snapshot_id),
            "analyzer": "ownership",
            "analyzer_version": saved.analyzer_version,
            "parameters": saved.parameters.model_dump(mode="json"),
            "created_at": saved.created_at.isoformat().replace("+00:00", "Z"),
            "finding_count": len(saved.findings),
        }
    ]
    assert listing["comparisons"] == []
    data = _run(session, "get_saved_analysis", number=1)
    assert data["saved"] is True
    assert [item["finding_id"] for item in data["findings"]["items"]] == [
        item.id for item in saved.findings
    ][:25]
    assert session.record.returned() == _expected_ids(saved, 25)
    assert _run(session, "get_saved_analysis", number=2)["code"] == ToolErrorCode.NOT_FOUND
    assert _run(session, "get_saved_analysis", number=0)["code"] == ToolErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize("tool", ["get_saved_analysis", "get_saved_comparison"])
def test_saved_results_are_unavailable_unless_selected(tool: str) -> None:
    session = ToolSession(load_snapshot(OWNERSHIP_SNAPSHOT))
    assert _run(session, tool, number=1)["code"] == ToolErrorCode.UNAVAILABLE
    assert _run(session, tool, number=1, path=str(OWNERSHIP_RESULT))["code"] == (
        ToolErrorCode.INVALID_ARGUMENTS
    )


def test_tampered_saved_analysis_fails_safely(tmp_path: Path) -> None:
    data = json.loads(OWNERSHIP_RESULT.read_text(encoding="utf-8"))
    data["findings"][0]["evidence_ids"] = ["fabricated-evidence"]
    data["findings"][0]["id"] = ""
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AnalysisValidationError) as raised:
        ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[tampered])
    assert "fabricated" not in str(raised.value)


def test_saved_analysis_for_another_snapshot_is_rejected() -> None:
    with pytest.raises(ToolSessionError):
        ToolSession.from_paths(SNAPSHOTS / "aws.json", analysis_paths=[OWNERSHIP_RESULT])


# ---- saved comparisons -------------------------------------------------------------


@pytest.fixture
def compared(tmp_path: Path) -> tuple[Path, Path, Path]:
    left = load_snapshot(OWNERSHIP_SNAPSHOT)
    data = left.model_dump(mode="json")
    data["snapshot_id"] = "00000000-0000-4000-8000-0000000000aa"
    for evidence in data["evidence"].values():
        evidence["snapshot_id"] = data["snapshot_id"]
    resource = next(iter(data["regions"].values()))["resources"][0]
    resource["attributes"] = {**resource.get("attributes", {}), "tags": {"Owner": "platform"}}
    right = validate_snapshot(data)
    left_path, right_path = tmp_path / "left.json", tmp_path / "right.json"
    left_path.write_text(left.model_dump_json(), encoding="utf-8")
    right_path.write_text(right.model_dump_json(), encoding="utf-8")
    result_path = tmp_path / "comparison.json"
    save_comparison(compare_snapshots(left, right), result_path, left=left, right=right)
    return left_path, right_path, result_path


def test_saved_comparisons_are_scoped_per_side(compared: tuple[Path, Path, Path]) -> None:
    left_path, right_path, result_path = compared
    session = ToolSession.from_paths(
        left_path, comparison_path=right_path, comparison_result_paths=[result_path]
    )
    listing = _run(session, "list_saved_results")["comparisons"]
    assert len(listing) == 1 and listing[0]["number"] == 1
    data = _run(session, "get_saved_comparison", number=1)
    left_id, right_id = session.index.snapshot_id, session.comparison_index.snapshot_id
    assert (data["left"]["snapshot_id"], data["right"]["snapshot_id"]) == (
        str(left_id),
        str(right_id),
    )
    matched = data["matched"]["items"]
    assert matched and matched[0]["differences"]
    for pair in matched:
        assert pair["left"]["snapshot_id"] == str(left_id)
        assert pair["right"]["snapshot_id"] == str(right_id)
    returned = session.record.returned()
    entries = {entry.snapshot_id: entry for entry in returned.snapshots}
    assert entries[left_id].resource_ids == tuple(sorted(p["left"]["resource_id"] for p in matched))
    assert entries[right_id].resource_ids == tuple(
        sorted(p["right"]["resource_id"] for p in matched)
    )
    assert _run(session, "get_saved_comparison", number=2)["code"] == ToolErrorCode.NOT_FOUND


def test_saved_comparisons_need_both_snapshots(compared: tuple[Path, Path, Path]) -> None:
    left_path, _, result_path = compared
    with pytest.raises(ToolSessionError):
        ToolSession.from_paths(left_path, comparison_result_paths=[result_path])


def test_tampered_saved_comparison_fails_safely(
    compared: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    left_path, right_path, result_path = compared
    data = json.loads(result_path.read_text(encoding="utf-8"))
    data["matched"][0]["differences"][0]["right_value"] = "fabricated"
    tampered = tmp_path / "tampered-comparison.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ComparisonValidationError):
        ToolSession.from_paths(
            left_path, comparison_path=right_path, comparison_result_paths=[tampered]
        )
