import json
from typing import Any

import pytest
from pydantic import ValidationError

from archdiscov.assistant.answers import SUBMIT_ANSWER, AnswerProposal, submit_answer_declaration
from archdiscov.assistant.conversation import MAX_TOOL_INPUT_SCHEMA_BYTES
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.registry import ToolDefinition
from tests.support.tool_audit import (
    assert_bounded_and_closed,
    assert_no_write_network_shell_or_path_inputs,
)

FINDING = {
    "kind": "analyzer_finding",
    "citations": [{"finding_ids": ["f-1"], "evidence_ids": ["e-1"]}],
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


# ---- the proposal contract ---------------------------------------------------------


@pytest.mark.parametrize(
    "data,code",
    [
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
                contexts=BOTH,
                conclusions=[{**DIFFERENCE, "citations": DIFFERENCE["citations"][:1]}],
            ),
            "answer_conclusion_shape",
        ),
        (_proposal(conclusions=[FINDING, FINDING]), "answer_duplicate"),
        (_proposal(conclusions=[]), "answer_status_shape"),
        (_proposal(conclusions=[DIFFERENCE]), "answer_context_missing"),
    ],
    ids=[
        "empty-citation",
        "finding-without-findings",
        "difference-one-side",
        "duplicate-conclusions",
        "supported-without-conclusions",
        "difference-without-comparison",
    ],
)
def test_proposals_that_do_not_fit_the_contract_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        AnswerProposal.model_validate(data)
    assert code in {error["type"] for error in raised.value.errors()}


# ---- the submit_answer declaration -------------------------------------------------


def test_declaration_is_derived_from_the_contract_and_passes_the_tool_audit() -> None:
    declaration = submit_answer_declaration()
    assert declaration.name == SUBMIT_ANSWER
    schema = declaration.input_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["status", "contexts", "summary"]
    assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema)
    assert len(json.dumps(schema)) <= MAX_TOOL_INPUT_SCHEMA_BYTES
    assert SUBMIT_ANSWER not in default_registry().tools
    tool = ToolDefinition(SUBMIT_ANSWER, declaration.description, AnswerProposal, lambda *_: None)
    assert tool.declaration == declaration
    assert_no_write_network_shell_or_path_inputs(tool)
    assert_bounded_and_closed(tool)
