"""End-to-end answers: fake-provider runs that use the tools, then submit a proposal.

A valid proposal is submitted for one analyzer; `REJECTION_SCENARIOS` holds one run per
`RejectionCode`, so a new code fails `test_every_rejection_code_is_covered` until a case is
added. Proposal IDs are computed beforehand from the same deterministic services the tools
call, as a model would copy them from tool results.
"""

from pathlib import Path

import pytest

from archdiscov.assistant.answer_validation import validate_proposal
from archdiscov.assistant.rejections import RejectionCode
from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind
from tests.support.answer_claims import INJECTION, service_result
from tests.support.answer_scenarios import (
    REJECTION_SCENARIOS,
    finding_proposal,
    proposal_status,
    proposed_run,
)
from tests.support.audit_scenarios import SCENARIOS
from tests.support.secrets import SECRET

# ---- valid answers -----------------------------------------------------------------


def test_a_valid_answer_validates() -> None:
    # `test_answer_rendering.py` validates every analyzer; the run is the same for each.
    kind = AnalyzerKind.SECURITY_GROUP_IMPACT
    scenario = SCENARIOS[kind]
    snapshot = scenario.build()
    session = ToolSession(snapshot, limits=ToolLimits(result_limit=100))
    findings = service_result(snapshot, kind, scenario.parameters, 3).findings
    result = proposed_run(
        session,
        [
            ("search_resources", {"limit": 5}),
            ("list_analyzers", {}),
            ("run_analyzer", {"kind": kind.value, "limit": 100, **scenario.parameters}),
        ],
        finding_proposal(session, findings, status=proposal_status(findings)),
    )
    assert validate_proposal(result.outcome.proposal, session) == ()


# ---- one run per rejection code ----------------------------------------------------


def test_every_rejection_code_is_covered() -> None:
    assert set(REJECTION_SCENARIOS) == set(RejectionCode)


@pytest.mark.parametrize("code", list(RejectionCode), ids=lambda code: code.value)
def test_each_rejection_is_reached_in_isolation(code: RejectionCode, tmp_path: Path) -> None:
    session, calls, proposal = REJECTION_SCENARIOS[code](tmp_path)
    result = proposed_run(session, calls, proposal)
    rejections = validate_proposal(result.outcome.proposal, session)
    assert {item.code for item in rejections} == {code}
    for item in rejections:
        assert SECRET not in item.model_dump_json()
        assert INJECTION.casefold() not in item.model_dump_json().casefold()
