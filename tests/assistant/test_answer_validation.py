import json
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.answer_validation import check_citations
from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.rejections import Rejection, RejectionCode
from archdiscov.assistant.tools.contracts import IdKind, ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services.analyses import save_analysis
from archdiscov.services.analysis_execution import run_analyzer
from archdiscov.services.snapshots import load_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import dispatch_tool
from tests.support.audit_scenarios import SCENARIOS
from tests.support.secrets import SECRET

OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
OWNERSHIP_RESULT = FIXTURES_ROOT / "analysis" / "ownership.json"


def _proposal(session: ToolSession, *conclusions: dict, **fields: Any) -> AnswerProposal:
    contexts = [{"side": "primary", "source_kind": session.index.source_kind.value}]
    if session.comparison_index is not None:
        contexts.append(
            {"side": "comparison", "source_kind": session.comparison_index.source_kind.value}
        )
    return AnswerProposal.model_validate(
        {
            "status": "partial",
            "contexts": contexts,
            "conclusions": list(conclusions),
            "summary": "Summary.",
            **fields,
        }
    )


def _finding_conclusion(item: dict, side: str = "primary") -> dict:
    return {
        "kind": "analyzer_finding",
        "citations": [
            {
                "side": side,
                "finding_ids": [item["finding_id"]],
                "evidence_ids": item["evidence_ids"],
            }
        ],
    }


def _codes(rejections: tuple[Rejection, ...]) -> list[tuple[str, str]]:
    return [(item.code.value, item.location) for item in rejections]


def _ownership() -> tuple[ToolSession, dict]:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    finding = dispatch_tool(session, "run_analyzer", kind="ownership")["findings"]["items"][0]
    return session, finding


# ---- valid proposals ---------------------------------------------------------------


def test_citing_delivered_findings_passes() -> None:
    # Citations are checked alike for every analyzer; this one's findings are mixed and limited.
    kind = AnalyzerKind.SECURITY_GROUP_IMPACT
    scenario = SCENARIOS[kind]
    session = ToolSession(scenario.build(), limits=ToolLimits(result_limit=100))
    findings = dispatch_tool(
        session, "run_analyzer", kind=kind.value, limit=100, **scenario.parameters
    )["findings"]["items"]
    assert findings
    proposal = _proposal(
        session,
        *(_finding_conclusion(item) for item in findings[:20]),
        limitations=[
            {"finding_id": item["finding_id"], "limitation": limitation["code"]}
            for item in findings[:20]
            for limitation in item["limitations"]
        ][:100],
    )
    assert check_citations(proposal, session) == ()


def test_citing_a_saved_analysis_passes() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    finding = dispatch_tool(session, "get_saved_analysis", number=1)["findings"]["items"][0]
    assert check_citations(_proposal(session, _finding_conclusion(finding)), session) == ()


# ---- comparison sessions -----------------------------------------------------------


@pytest.fixture
def compared(tmp_path: Path) -> ToolSession:
    """Left is the ownership snapshot; right renames its resource and has a saved result."""
    left = load_snapshot(OWNERSHIP_SNAPSHOT)
    data = left.model_dump(mode="json")
    data["snapshot_id"] = "00000000-0000-4000-8000-0000000000cc"
    for evidence in data["evidence"].values():
        evidence["snapshot_id"] = data["snapshot_id"]
    right = validate_snapshot(json.loads(json.dumps(data).replace("vpc-owner", "vpc-renamed")))
    left_path, right_path = tmp_path / "left.json", tmp_path / "right.json"
    left_path.write_text(left.model_dump_json(), encoding="utf-8")
    right_path.write_text(right.model_dump_json(), encoding="utf-8")
    result_path = tmp_path / "right-ownership.json"
    save_analysis(
        run_analyzer(GraphIndex(right), {"kind": "ownership"}), result_path, snapshot=right
    )
    return ToolSession.from_paths(
        left_path, comparison_path=right_path, analysis_paths=[result_path]
    )


def test_each_side_cites_only_its_own_snapshot(compared: ToolSession) -> None:
    left_resource = compared.index.resource_ids[0]
    right_resource = compared.comparison_index.resource_ids[0]
    compared.record.record(compared.index.snapshot_id, IdKind.RESOURCE, [left_resource])
    compared.record.record(compared.comparison_index.snapshot_id, IdKind.RESOURCE, [right_resource])
    difference = _proposal(
        compared,
        {
            "kind": "recorded_difference",
            "citations": [
                {"side": "primary", "resource_ids": [left_resource]},
                {"side": "comparison", "resource_ids": [right_resource]},
            ],
        },
    )
    assert check_citations(difference, compared) == ()
    finding = dispatch_tool(compared, "get_saved_analysis", number=1)["findings"]["items"][0]
    swapped = _proposal(
        compared,
        {"kind": "analyzer_finding", "citations": [{"finding_ids": [finding["finding_id"]]}]},
        {
            "kind": "recorded_configuration",
            "citations": [{"evidence_ids": ["tags"], "resource_ids": [right_resource]}],
        },
    )
    codes = _codes(check_citations(swapped, compared))
    assert ("answer_other_snapshot_id", "conclusions[0].citations[0].finding_ids[0]") in codes
    assert ("answer_other_snapshot_id", "conclusions[1].citations[0].resource_ids[0]") in codes


# ---- isolated rejections -----------------------------------------------------------


def test_ids_present_but_not_returned_are_rejected() -> None:
    # The saved result is selected for the session but never delivered by a tool.
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    finding_id = session.analyses[0].findings[0].id
    resource = session.index.resource_ids[0]
    evidence = session.index.evidence_ids[0]
    proposal = _proposal(
        session,
        {"kind": "analyzer_finding", "citations": [{"finding_ids": [finding_id]}]},
        {
            "kind": "recorded_configuration",
            "citations": [{"evidence_ids": [evidence], "resource_ids": [resource]}],
        },
    )
    assert _codes(check_citations(proposal, session)) == [
        ("answer_id_not_returned", "conclusions[0].citations[0].finding_ids[0]"),
        ("answer_id_not_returned", "conclusions[1].citations[0].evidence_ids[0]"),
        ("answer_id_not_returned", "conclusions[1].citations[0].resource_ids[0]"),
    ]


def _configuration(evidence: str, *resources: str) -> dict:
    return {
        "kind": "recorded_configuration",
        "citations": [{"evidence_ids": [evidence], "resource_ids": list(resources)}],
    }


def test_evidence_must_be_cited_with_its_own_resource() -> None:
    session = ToolSession.from_paths(FIXTURES_ROOT / "snapshots" / "network.json")
    index = session.index
    first, second = [
        resource for resource in index.resource_ids if index.resource_evidence_ids(resource)
    ][:2]
    for resource in (first, second):
        dispatch_tool(session, "inspect_resource", resource_id=resource)
    evidence = index.resource_evidence_ids(first)[0]
    detached = ("answer_evidence_detached", "conclusions[0].citations[0].evidence_ids[0]")
    for resources in ((second,), ()):
        proposal = _proposal(session, _configuration(evidence, *resources))
        assert _codes(check_citations(proposal, session)) == [detached]
    attached = _proposal(session, _configuration(evidence, first, second))
    assert check_citations(attached, session) == ()


def test_a_comparison_side_without_a_comparison_snapshot_is_rejected() -> None:
    session, finding = _ownership()
    proposal = AnswerProposal.model_validate(
        {
            "status": "partial",
            "contexts": [
                {"side": "primary", "source_kind": session.index.source_kind.value},
                {"side": "comparison", "source_kind": "aws"},
            ],
            "conclusions": [_finding_conclusion(finding, side="comparison")],
            "limitations": [
                {"side": "comparison", "finding_id": finding["finding_id"], "limitation": "x"}
            ],
            "summary": "s",
        }
    )
    assert _codes(check_citations(proposal, session)) == [
        ("answer_side_unavailable", "conclusions[0].citations[0].side"),
        ("answer_side_unavailable", "contexts[1].side"),
        ("answer_side_unavailable", "limitations[0].side"),
    ]


def test_all_rejections_are_collected_in_location_order() -> None:
    session, finding = _ownership()
    proposal = _proposal(
        session,
        {
            "kind": "analyzer_finding",
            "citations": [{"finding_ids": ["x", finding["finding_id"], "y"]}],
        },
        {
            "kind": "recorded_configuration",
            "citations": [{"evidence_ids": ["e"], "resource_ids": ["r"]}],
        },
        limitations=[{"finding_id": "z", "limitation": "c"}],
    )
    assert _codes(check_citations(proposal, session)) == [
        ("answer_unknown_id", "conclusions[0].citations[0].finding_ids[0]"),
        ("answer_unknown_id", "conclusions[0].citations[0].finding_ids[2]"),
        ("answer_unknown_id", "conclusions[1].citations[0].evidence_ids[0]"),
        ("answer_unknown_id", "conclusions[1].citations[0].resource_ids[0]"),
        ("answer_unknown_id", "limitations[0].finding_id"),
    ]


def test_rejections_carry_fixed_messages_only() -> None:
    with pytest.raises(ValueError):
        Rejection(code=RejectionCode.UNKNOWN_ID, location="conclusions[0]", message=SECRET)


def test_validation_does_not_change_the_run_record() -> None:
    session, finding = _ownership()
    before = session.record.context()
    check_citations(_proposal(session, _finding_conclusion(finding)), session)
    check_citations(
        _proposal(session, {"kind": "analyzer_finding", "citations": [{"finding_ids": ["x"]}]}),
        session,
    )
    assert session.record.context().model_dump_json() == before.model_dump_json()
