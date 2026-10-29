import json
from collections import defaultdict
from pathlib import Path

import pytest

from archdiscov.assistant.answer_validation import (
    MIN_NAMED_ID_CHARS,
    MIN_REPEATED_TEXT_CHARS,
    check_claims,
    validate_proposal,
)
from archdiscov.assistant.answers import combined_status
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind, SnapshotSourceKind
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import (
    INJECTION,
    analyzed_findings,
    cited_proposal,
    dispatch_tool,
    expected_status,
)

SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"
RANK = {"insufficient_evidence": 0, "partial": 1, "supported": 2}


def _codes(rejections) -> list[tuple[str, str]]:
    return [(item.code.value, item.location) for item in rejections]


# ---- valid proposals ---------------------------------------------------------------


def test_honest_proposals_pass_with_their_limitations_acknowledged() -> None:
    # `test_answer_rendering.py` validates an honest proposal for every analyzer.
    session, findings = analyzed_findings(AnalyzerKind.CROSS_ACCOUNT_TRUST, "failed")
    status = expected_status(findings)
    proposal = cited_proposal(session, findings, status=status)
    assert validate_proposal(proposal, session) == ()
    for weaker in (value for value in RANK if RANK[value] < RANK[status]):
        assert validate_proposal(cited_proposal(session, findings, status=weaker), session) == ()


# ---- status ------------------------------------------------------------------------


def test_status_follows_the_weakest_mix_of_cited_findings() -> None:
    session, findings = analyzed_findings(AnalyzerKind.SECURITY_GROUP_IMPACT)
    by_status = defaultdict(list)
    for item in findings:
        by_status[item["status"]].append(item)
    supported = by_status["supported"][:1]
    insufficient = by_status["insufficient_evidence"][:1]
    assert supported and insufficient
    assert check_claims(cited_proposal(session, supported, status="supported"), session) == ()
    mixed = cited_proposal(session, supported + insufficient, status="supported")
    assert ("answer_status_upgrade", "status") in _codes(check_claims(mixed, session))
    only_insufficient = cited_proposal(session, insufficient, status="partial")
    assert ("answer_status_upgrade", "status") in _codes(check_claims(only_insufficient, session))


def test_configuration_alone_cannot_support_an_answer() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    resource = session.index.resource_ids[0]
    evidence = [
        item["evidence_id"]
        for item in dispatch_tool(session, "inspect_resource", resource_id=resource)["evidence"][
            "items"
        ]
    ]
    conclusion = {
        "kind": "recorded_configuration",
        "citations": [{"evidence_ids": evidence, "resource_ids": [resource]}],
    }
    proposal = cited_proposal(session, [], status="partial", conclusions=[conclusion])
    assert _codes(check_claims(proposal, session)) == [("answer_status_upgrade", "status")]
    insufficient = cited_proposal(
        session, [], status="insufficient_evidence", conclusions=[conclusion]
    )
    assert validate_proposal(insufficient, session) == ()


# ---- limitations -------------------------------------------------------------------


def test_every_limitation_of_a_cited_finding_must_be_acknowledged() -> None:
    session, findings = analyzed_findings(AnalyzerKind.DATABASE_ACCESS, "failed")
    limited = [item for item in findings if item["limitations"]]
    proposal = cited_proposal(session, limited, status="insufficient_evidence")
    dropped = proposal.model_copy(update={"limitations": proposal.limitations[1:]})
    rejected = _codes(check_claims(dropped, session))
    assert rejected == [
        ("answer_limitation_unacknowledged", "conclusions[0].citations[0].finding_ids[0]")
    ]


def test_acknowledging_a_code_the_finding_lacks_is_rejected() -> None:
    session, findings = analyzed_findings(AnalyzerKind.OWNERSHIP, "failed")
    proposal = cited_proposal(
        session,
        findings,
        status="partial",
        limitations=[
            {"finding_id": findings[0]["finding_id"], "limitation": code}
            for code in ["coverage_failed", "not_a_limitation_of_this_finding"]
        ],
    )
    assert _codes(check_claims(proposal, session)) == [
        ("answer_limitation_unknown", "limitations[1].limitation")
    ]


# ---- contradictions ----------------------------------------------------------------


def _contradicting_pair(session: ToolSession, findings: list[dict]) -> tuple[dict, dict]:
    recorded = {item.finding_id: item for item in session.record.context().findings}
    groups = defaultdict(list)
    for item in findings:
        groups[recorded[item["finding_id"]].primary_resource_id].append(item)
    for items in groups.values():
        conclusions = {
            (recorded[item["finding_id"]].status, recorded[item["finding_id"]].decision): item
            for item in items
        }
        if len(conclusions) > 1:
            first, second = list(conclusions.values())[:2]
            return first, second
    raise AssertionError("no contradicting findings")


def test_omitting_a_contradicting_finding_is_rejected() -> None:
    session, findings = analyzed_findings(AnalyzerKind.SECURITY_GROUP_IMPACT)
    first, second = _contradicting_pair(session, findings)
    status = expected_status([first])
    assert _codes(check_claims(cited_proposal(session, [first], status=status), session)) == [
        ("answer_contradiction_omitted", "conclusions[0].citations[0].finding_ids[0]")
    ]
    both = cited_proposal(session, [first, second], status=expected_status([first, second]))
    assert "answer_contradiction_omitted" not in {
        code for code, _ in _codes(check_claims(both, session))
    }


def test_findings_about_other_resources_are_not_contradictions() -> None:
    session, findings = analyzed_findings(AnalyzerKind.DATABASE_ACCESS)
    first = findings[0]
    proposal = cited_proposal(session, [first], status=expected_status([first]))
    assert check_claims(proposal, session) == ()


# ---- planned as deployed -----------------------------------------------------------


def test_declared_architecture_described_as_observed_is_rejected() -> None:
    session = ToolSession.from_paths(OWNERSHIP_SNAPSHOT)
    session.record.record_snapshot(
        session.index.snapshot_id,
        SnapshotSourceKind.TERRAFORM_PLAN,
        session.index.collection_window,
    )
    findings = dispatch_tool(session, "run_analyzer", kind="ownership")["findings"]["items"]
    status = expected_status(findings)
    observed = cited_proposal(
        session, findings, status=status, contexts=[{"side": "primary", "source_kind": "aws"}]
    )
    assert _codes(validate_proposal(observed, session)) == [
        ("answer_planned_as_deployed", "contexts[0].source_kind")
    ]
    # Another declared kind is a plain mismatch, not declared state presented as observed.
    declared = cited_proposal(
        session,
        findings,
        status=status,
        contexts=[{"side": "primary", "source_kind": "cloudformation"}],
    )
    assert _codes(validate_proposal(declared, session)) == [
        ("answer_source_kind_mismatch", "contexts[0].source_kind")
    ]


# ---- narrative ---------------------------------------------------------------------


@pytest.fixture
def injected(tmp_path: Path) -> ToolSession:
    data = json.loads((SNAPSHOTS / "aws.json").read_text(encoding="utf-8"))
    vpc = data["regions"]["eu-west-1"]["resources"][0]
    vpc["name"] = INJECTION
    vpc["attributes"] = {"tags": {"Team": "web"}}
    path = tmp_path / "injected.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    session = ToolSession.from_paths(path)
    dispatch_tool(session, "search_resources")
    return session


def test_narrative_may_paraphrase_but_not_repeat_imported_text(injected: ToolSession) -> None:
    # Case, whitespace, and partial stretches are the fingerprint's concern
    # (tools/test_run_context.py); here the summary and the explanation are each checked.
    repeated = f"The name says: {INJECTION}."
    proposal = cited_proposal(injected, [], status="insufficient_evidence", summary=repeated)
    assert _codes(check_claims(proposal, injected)) == [
        ("answer_narrative_imported_text", "summary")
    ]
    explained = cited_proposal(injected, [], status="insufficient_evidence", explanation=repeated)
    assert _codes(check_claims(explained, injected)) == [
        ("answer_narrative_imported_text", "explanation")
    ]
    assert len("web") < MIN_REPEATED_TEXT_CHARS
    paraphrased = cited_proposal(
        injected,
        [],
        status="insufficient_evidence",
        summary="The web team owns a VPC whose name contains instruction-like text.",
    )
    assert check_claims(paraphrased, injected) == ()


def test_narrative_may_name_cited_ids_but_not_uncited_ones() -> None:
    session, findings = analyzed_findings(AnalyzerKind.OWNERSHIP)
    status = expected_status(findings)
    finding_id = findings[0]["finding_id"]
    cited = cited_proposal(
        session, findings, status=status, summary=f"Finding {finding_id} uses the tags evidence."
    )
    assert check_claims(cited, session) == ()
    # The proposal cites only finding IDs, so any resource ID is uncited.
    resource = session.index.resource_ids[0]
    assert len(resource) >= MIN_NAMED_ID_CHARS
    uncited = cited_proposal(session, findings, status=status, summary=f"See {resource}.")
    assert _codes(check_claims(uncited, session)) == [("answer_narrative_uncited_id", "summary")]


# ---- combined ----------------------------------------------------------------------


def test_validate_proposal_reports_citation_and_claim_failures_together() -> None:
    session, findings = analyzed_findings(AnalyzerKind.DATABASE_ACCESS, "failed")
    honest = cited_proposal(session, findings, status="insufficient_evidence")
    proposal = cited_proposal(
        session,
        findings,
        status="supported",
        conclusions=[
            *[conclusion.model_dump(mode="json") for conclusion in honest.conclusions],
            {"kind": "analyzer_finding", "citations": [{"finding_ids": ["fabricated"]}]},
        ],
    )
    assert _codes(validate_proposal(proposal, session)) == [
        ("answer_unknown_id", "conclusions[1].citations[0].finding_ids[0]"),
        ("answer_status_upgrade", "status"),
    ]


def test_a_combined_status_needs_at_least_one_finding() -> None:
    with pytest.raises(ValueError):
        combined_status(())
    assert combined_status(["supported", "supported"]) == "supported"
    assert combined_status(["insufficient_evidence"] * 2) == "insufficient_evidence"
    assert combined_status(["supported", "insufficient_evidence"]) == "partial"
