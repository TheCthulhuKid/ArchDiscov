import json
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.answer_validation import check_citations
from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import ParsedToolArguments, ToolCall
from archdiscov.assistant.rejections import REJECTION_MESSAGES, Rejection, RejectionCode
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.contracts import IdKind, ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services.analyses import save_analysis
from archdiscov.services.analysis_execution import run_analyzer
from archdiscov.services.snapshots import load_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.audit_scenarios import SCENARIOS

REGISTRY = default_registry()
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
OWNERSHIP_RESULT = FIXTURES_ROOT / "analysis" / "ownership.json"
SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"


def _call(session: ToolSession, tool: str, **arguments: Any) -> dict:
    result = REGISTRY.dispatch(
        session, ToolCall(id="c", name=tool, arguments=ParsedToolArguments(value=arguments))
    )
    return json.loads(result.content)


def _proposal(
    session: ToolSession, *conclusions: dict, contexts: list | None = None, **fields: Any
) -> AnswerProposal:
    if contexts is not None:
        return AnswerProposal.model_validate(
            {
                "status": "partial",
                "contexts": contexts,
                "conclusions": list(conclusions),
                "summary": "Summary.",
                **fields,
            }
        )
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
    finding = _call(session, "run_analyzer", kind="ownership")["findings"]["items"][0]
    return session, finding


# ---- valid proposals ---------------------------------------------------------------


@pytest.mark.parametrize("kind", list(AnalyzerKind))
def test_citing_delivered_findings_passes_for_every_analyzer(kind: AnalyzerKind) -> None:
    scenario = SCENARIOS[kind]
    session = ToolSession(scenario.build(), limits=ToolLimits(result_limit=100))
    findings = _call(session, "run_analyzer", kind=kind.value, limit=100, **scenario.parameters)[
        "findings"
    ]["items"]
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


def test_citing_queried_configuration_passes() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    resource = session.index.resource_ids[0]
    inspected = _call(session, "inspect_resource", resource_id=resource)
    evidence = [item["evidence_id"] for item in inspected["evidence"]["items"]]
    proposal = _proposal(
        session,
        {
            "kind": "recorded_configuration",
            "citations": [{"evidence_ids": evidence, "resource_ids": [resource]}],
        },
    )
    assert check_citations(proposal, session) == ()


def test_citing_a_saved_analysis_passes() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT, analysis_paths=[OWNERSHIP_RESULT])
    finding = _call(session, "get_saved_analysis", number=1)["findings"]["items"][0]
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


def test_a_valid_difference_cites_each_side(compared: ToolSession) -> None:
    left_resource = compared.index.resource_ids[0]
    right_resource = compared.comparison_index.resource_ids[0]
    compared.record.record(compared.index.snapshot_id, IdKind.RESOURCE, [left_resource])
    compared.record.record(compared.comparison_index.snapshot_id, IdKind.RESOURCE, [right_resource])
    proposal = _proposal(
        compared,
        {
            "kind": "recorded_difference",
            "citations": [
                {"side": "primary", "resource_ids": [left_resource]},
                {"side": "comparison", "resource_ids": [right_resource]},
            ],
        },
    )
    assert check_citations(proposal, compared) == ()


def test_ids_from_the_other_snapshot_are_rejected(compared: ToolSession) -> None:
    right_resource = compared.comparison_index.resource_ids[0]
    compared.record.record(compared.comparison_index.snapshot_id, IdKind.RESOURCE, [right_resource])
    finding = _call(compared, "get_saved_analysis", number=1)["findings"]["items"][0]
    proposal = _proposal(
        compared,
        {"kind": "analyzer_finding", "citations": [{"finding_ids": [finding["finding_id"]]}]},
        {
            "kind": "recorded_configuration",
            "citations": [{"evidence_ids": ["tags"], "resource_ids": [right_resource]}],
        },
    )
    codes = _codes(check_citations(proposal, compared))
    assert ("answer_other_snapshot_id", "conclusions[0].citations[0].finding_ids[0]") in codes
    assert ("answer_other_snapshot_id", "conclusions[1].citations[0].resource_ids[0]") in codes


# ---- isolated rejections -----------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("finding_ids", f"fabricated-{SECRET}"),
        ("evidence_ids", "fabricated"),
        ("resource_ids", "fabricated"),
    ],
)
def test_fabricated_ids_are_unknown(field: str, value: str) -> None:
    session, finding = _ownership()
    citation = {"finding_ids": [finding["finding_id"]], field: [value]}
    if field == "finding_ids":
        citation = {"finding_ids": [value]}
    proposal = _proposal(session, {"kind": "analyzer_finding", "citations": [citation]})
    rejections = check_citations(proposal, session)
    assert _codes(rejections) == [("answer_unknown_id", f"conclusions[0].citations[0].{field}[0]")]
    assert SECRET not in json.dumps([item.model_dump(mode="json") for item in rejections])


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


def _two_inspected_resources() -> tuple[ToolSession, str, str]:
    session = ToolSession.from_paths(FIXTURES_ROOT / "snapshots" / "network.json")
    index = session.index
    first, second = [
        resource for resource in index.resource_ids if index.resource_evidence_ids(resource)
    ][:2]
    for resource in (first, second):
        _call(session, "inspect_resource", resource_id=resource)
    return session, first, second


def _configuration(evidence: str, *resources: str) -> dict:
    return {
        "kind": "recorded_configuration",
        "citations": [{"evidence_ids": [evidence], "resource_ids": list(resources)}],
    }


def test_evidence_cited_for_another_resource_is_detached() -> None:
    session, first, second = _two_inspected_resources()
    evidence = session.index.resource_evidence_ids(first)[0]
    proposal = _proposal(session, _configuration(evidence, second))
    assert _codes(check_citations(proposal, session)) == [
        ("answer_evidence_detached", "conclusions[0].citations[0].evidence_ids[0]")
    ]


def test_evidence_cited_without_any_resource_or_finding_is_detached() -> None:
    session, first, _ = _two_inspected_resources()
    proposal = _proposal(session, _configuration(session.index.resource_evidence_ids(first)[0]))
    assert _codes(check_citations(proposal, session)) == [
        ("answer_evidence_detached", "conclusions[0].citations[0].evidence_ids[0]")
    ]


def test_evidence_cited_with_its_own_resource_passes() -> None:
    session, first, second = _two_inspected_resources()
    evidence = session.index.resource_evidence_ids(first)[0]
    assert (
        check_citations(_proposal(session, _configuration(evidence, first, second)), session) == ()
    )


def test_evidence_attached_to_a_cited_finding_passes_without_its_resource() -> None:
    session, finding = _ownership()
    proposal = _proposal(session, _finding_conclusion(finding))
    assert check_citations(proposal, session) == ()


def test_a_mismatched_source_kind_is_rejected() -> None:
    session, finding = _ownership()
    proposal = _proposal(
        session,
        _finding_conclusion(finding),
        contexts=[{"side": "primary", "source_kind": "terraform_plan"}],
    )
    assert _codes(check_citations(proposal, session)) == [
        ("answer_source_kind_mismatch", "contexts[0].source_kind")
    ]


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


def test_acknowledged_limitations_must_name_returned_findings() -> None:
    session, finding = _ownership()
    proposal = _proposal(
        session,
        _finding_conclusion(finding),
        limitations=[{"finding_id": "fabricated", "limitation": "coverage_failed"}],
    )
    assert _codes(check_citations(proposal, session)) == [
        ("answer_unknown_id", "limitations[0].finding_id")
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
    assert set(REJECTION_MESSAGES) == set(RejectionCode)
    with pytest.raises(ValueError):
        Rejection(code=RejectionCode.UNKNOWN_ID, location="conclusions[0]", message=SECRET)
    with pytest.raises(ValueError):
        Rejection(
            code=RejectionCode.UNKNOWN_ID,
            location=f"conclusions[0].{SECRET}",
            message=REJECTION_MESSAGES[RejectionCode.UNKNOWN_ID],
        )


def test_validation_does_not_change_the_run_record() -> None:
    session, finding = _ownership()
    before = session.record.context()
    check_citations(_proposal(session, _finding_conclusion(finding)), session)
    check_citations(
        _proposal(session, {"kind": "analyzer_finding", "citations": [{"finding_ids": ["x"]}]}),
        session,
    )
    assert session.record.context().model_dump_json() == before.model_dump_json()
