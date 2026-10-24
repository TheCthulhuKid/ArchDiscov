import json
from typing import Any

import pytest
from pydantic import ValidationError

from archdiscov.assistant.answers import (
    MAX_CITED_IDS,
    MAX_CONCLUSIONS,
    MAX_EXPLANATION_CHARS,
    MAX_LIMITATIONS,
    MAX_SUMMARY_CHARS,
    SUBMIT_ANSWER,
    AnswerProposal,
    AnswerSide,
    submit_answer_declaration,
)
from archdiscov.assistant.conversation import (
    MAX_TOOL_INPUT_SCHEMA_BYTES,
    Conversation,
    UserMessage,
    parse_tool_arguments,
)
from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.provider import CompletionRequest, complete
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.registry import ToolDefinition
from archdiscov.models.enums import AnalysisStatus
from tests.support.tool_audit import (
    assert_bounded_and_closed,
    assert_no_write_network_shell_or_path_inputs,
)

FINDING = {
    "kind": "analyzer_finding",
    "citations": [{"finding_ids": ["f-1"], "evidence_ids": ["e-1"]}],
}
CONFIG = {
    "kind": "recorded_configuration",
    "citations": [{"evidence_ids": ["e-2"], "resource_ids": ["r-1"]}],
}
DIFFERENCE = {
    "kind": "recorded_difference",
    "citations": [
        {"side": "primary", "resource_ids": ["r-1"]},
        {"side": "comparison", "resource_ids": ["r-1"], "evidence_ids": ["e-9"]},
    ],
}
PRIMARY = [{"side": "primary", "source_kind": "aws"}]
BOTH = [*PRIMARY, {"side": "comparison", "source_kind": "terraform_plan"}]


def _proposal(**fields: Any) -> dict:
    return {
        "status": "supported",
        "contexts": PRIMARY,
        "conclusions": [FINDING],
        "summary": "One database is reachable from one function.",
        **fields,
    }


def _rejected(data: dict) -> set[str]:
    with pytest.raises(ValidationError) as raised:
        AnswerProposal.model_validate(data)
    return {error["type"] for error in raised.value.errors()}


# ---- valid shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        _proposal(),
        _proposal(
            status="partial",
            conclusions=[FINDING, CONFIG],
            limitations=[{"finding_id": "f-1", "limitation": "coverage_failed"}],
            explanation="Coverage of one region failed.",
        ),
        _proposal(contexts=BOTH, conclusions=[DIFFERENCE]),
        _proposal(
            status="insufficient_evidence",
            conclusions=[],
            insufficient_reason="no_applicable_findings",
        ),
        _proposal(
            status="insufficient_evidence",
            conclusions=[CONFIG],
            insufficient_reason="limited_coverage",
        ),
    ],
    ids=["supported", "partial", "difference", "insufficient", "insufficient-with-context"],
)
def test_valid_proposals_round_trip(data: dict) -> None:
    proposal = AnswerProposal.model_validate(data)
    assert AnswerProposal.model_validate_json(proposal.model_dump_json()) == proposal
    assert proposal.status in set(AnalysisStatus)


def test_proposals_are_frozen_and_default_to_the_primary_side() -> None:
    proposal = AnswerProposal.model_validate(_proposal())
    assert proposal.conclusions[0].citations[0].side == AnswerSide.PRIMARY
    with pytest.raises(ValidationError):
        proposal.status = AnalysisStatus.PARTIAL


# ---- bounds ------------------------------------------------------------------------


def _ids(count: int) -> list[str]:
    return [f"f-{index}" for index in range(count)]


@pytest.mark.parametrize(
    "data",
    [
        _proposal(
            conclusions=[
                {**FINDING, "citations": [{"finding_ids": _ids(index + 1)}]}
                for index in range(MAX_CONCLUSIONS + 1)
            ]
        ),
        _proposal(
            conclusions=[
                {
                    "kind": "analyzer_finding",
                    "citations": [{"finding_ids": _ids(MAX_CITED_IDS + 1)}],
                }
            ]
        ),
        _proposal(
            limitations=[
                {"finding_id": f"f-{index}", "limitation": "c"}
                for index in range(MAX_LIMITATIONS + 1)
            ]
        ),
        _proposal(contexts=[*BOTH, {"side": "primary", "source_kind": "aws"}]),
        _proposal(summary="s" * (MAX_SUMMARY_CHARS + 1)),
        _proposal(summary=""),
        _proposal(summary="   "),
        _proposal(explanation="e" * (MAX_EXPLANATION_CHARS + 1)),
        _proposal(explanation=""),
        _proposal(
            conclusions=[
                {"kind": "analyzer_finding", "citations": [{"finding_ids": ["x" * 2_049]}]}
            ]
        ),
        _proposal(limitations=[{"finding_id": "f-1", "limitation": "Not A Code"}]),
        _proposal(status="certain"),
        _proposal(contexts=[{"side": "primary", "source_kind": "guess"}]),
        _proposal(conclusions=[{"kind": "opinion", "citations": [{"finding_ids": ["f-1"]}]}]),
    ],
    ids=[
        "conclusions",
        "ids",
        "limitations",
        "contexts",
        "summary-long",
        "summary-empty",
        "summary-blank",
        "explanation-long",
        "explanation-empty",
        "id-long",
        "code",
        "status",
        "source-kind",
        "conclusion-kind",
    ],
)
def test_every_bound_is_enforced(data: dict) -> None:
    assert _rejected(data)


@pytest.mark.parametrize(
    "data,code",
    [
        (_proposal(conclusions=[{"kind": "analyzer_finding", "citations": []}]), "too_short"),
        (
            _proposal(conclusions=[{"kind": "analyzer_finding", "citations": [{}]}]),
            "answer_citation_empty",
        ),
        (
            _proposal(
                conclusions=[{"kind": "analyzer_finding", "citations": [{"evidence_ids": ["e"]}]}]
            ),
            "answer_conclusion_shape",
        ),
        (
            _proposal(
                conclusions=[
                    {"kind": "recorded_configuration", "citations": [{"resource_ids": ["r"]}]}
                ]
            ),
            "answer_conclusion_shape",
        ),
        (
            _proposal(
                contexts=BOTH,
                conclusions=[{**DIFFERENCE, "citations": DIFFERENCE["citations"][:1]}],
            ),
            "answer_conclusion_shape",
        ),
        (
            _proposal(
                contexts=BOTH,
                conclusions=[
                    {
                        **FINDING,
                        "citations": FINDING["citations"]
                        + [{"side": "comparison", "finding_ids": ["f-2"]}],
                    }
                ],
            ),
            "answer_conclusion_shape",
        ),
        (
            _proposal(
                contexts=BOTH,
                conclusions=[{**DIFFERENCE, "citations": [DIFFERENCE["citations"][0]] * 2}],
            ),
            "answer_duplicate",
        ),
    ],
    ids=[
        "no-citation",
        "empty-citation",
        "finding-without-findings",
        "config-without-evidence",
        "difference-one-side",
        "two-sides",
        "duplicate-sides",
    ],
)
def test_citations_must_fit_the_conclusion(data: dict, code: str) -> None:
    assert code in _rejected(data)


@pytest.mark.parametrize(
    "data",
    [
        _proposal(
            conclusions=[
                {"kind": "analyzer_finding", "citations": [{"finding_ids": ["f-1", "f-1"]}]}
            ]
        ),
        _proposal(
            conclusions=[
                {"kind": "recorded_configuration", "citations": [{"evidence_ids": ["e", "e"]}]}
            ]
        ),
        _proposal(
            conclusions=[
                {**CONFIG, "citations": [{"evidence_ids": ["e"], "resource_ids": ["r", "r"]}]}
            ]
        ),
        _proposal(conclusions=[FINDING, FINDING]),
        _proposal(limitations=[{"finding_id": "f-1", "limitation": "c"}] * 2),
        _proposal(contexts=PRIMARY * 2),
    ],
    ids=["finding-ids", "evidence-ids", "resource-ids", "conclusions", "limitations", "contexts"],
)
def test_duplicates_are_rejected(data: dict) -> None:
    assert "answer_duplicate" in _rejected(data)


@pytest.mark.parametrize(
    "data,code",
    [
        (_proposal(status="insufficient_evidence", conclusions=[]), "answer_status_shape"),
        (_proposal(insufficient_reason="out_of_scope"), "answer_status_shape"),
        (_proposal(conclusions=[]), "answer_status_shape"),
        (_proposal(status="partial", conclusions=[]), "answer_status_shape"),
        (
            _proposal(
                status="insufficient_evidence", conclusions=[], insufficient_reason="because"
            ),
            "enum",
        ),
    ],
    ids=[
        "insufficient-without-reason",
        "supported-with-reason",
        "supported-without-conclusions",
        "partial-without-conclusions",
        "unknown-reason",
    ],
)
def test_status_and_reason_must_agree(data: dict, code: str) -> None:
    assert code in _rejected(data)


@pytest.mark.parametrize(
    "data",
    [
        _proposal(contexts=[{"side": "comparison", "source_kind": "aws"}]),
        _proposal(conclusions=[DIFFERENCE]),
        _proposal(limitations=[{"side": "comparison", "finding_id": "f-1", "limitation": "c"}]),
        _proposal(contexts=[]),
    ],
    ids=["no-primary", "difference-without-comparison", "limitation-side", "none"],
)
def test_every_side_needs_a_context(data: dict) -> None:
    assert _rejected(data) & {"answer_context_missing", "too_short"}


@pytest.mark.parametrize(
    "data",
    [
        _proposal(confidence=0.9),
        _proposal(contexts=[{**PRIMARY[0], "snapshot_id": "x"}]),
        _proposal(conclusions=[{**FINDING, "claim": "Everything is public."}]),
        _proposal(conclusions=[{**FINDING, "citations": [{"finding_ids": ["f"], "url": "x"}]}]),
        _proposal(limitations=[{"finding_id": "f", "limitation": "c", "message": "fine"}]),
    ],
    ids=["top", "context", "conclusion", "citation", "limitation"],
)
def test_unknown_fields_are_rejected(data: dict) -> None:
    assert "extra_forbidden" in _rejected(data)


# ---- the submit_answer declaration -------------------------------------------------


def test_declaration_is_derived_from_the_contract() -> None:
    declaration = submit_answer_declaration()
    assert declaration.name == SUBMIT_ANSWER
    schema = declaration.input_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["status", "contexts", "summary"]
    assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema)
    assert len(json.dumps(schema)) <= MAX_TOOL_INPUT_SCHEMA_BYTES
    assert SUBMIT_ANSWER not in default_registry().tools


def _as_tool() -> ToolDefinition:
    return ToolDefinition(
        SUBMIT_ANSWER, submit_answer_declaration().description, AnswerProposal, lambda *_: None
    )


def test_declaration_passes_the_tool_schema_audit() -> None:
    tool = _as_tool()
    assert tool.declaration == submit_answer_declaration()
    assert_no_write_network_shell_or_path_inputs(tool)
    assert_bounded_and_closed(tool)


def test_declaration_is_valid_for_providers_alongside_the_tools() -> None:
    tools = (*default_registry().declarations(), submit_answer_declaration())
    request = CompletionRequest(
        conversation=Conversation(messages=(UserMessage(text="What is exposed?"),)), tools=tools
    )
    transcript = FakeTranscript.model_validate(
        {
            "turns": [
                {
                    "kind": "respond",
                    "expect": {"tool_names": [tool.name for tool in tools]},
                    "response": {
                        "message": {"role": "assistant", "text": "ok"},
                        "stop_reason": "end",
                    },
                }
            ]
        }
    )
    assert complete(FakeProvider(transcript), request).message.text == "ok"


def test_provider_arguments_parse_into_a_proposal() -> None:
    arguments = parse_tool_arguments(json.dumps(_proposal()))
    assert arguments.kind == "parsed"
    assert AnswerProposal.model_validate(arguments.value) == AnswerProposal.model_validate(
        _proposal()
    )
