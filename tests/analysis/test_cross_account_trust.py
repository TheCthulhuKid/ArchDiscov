import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    TrustClassification,
    TrustEffect,
    TrustSource,
)
from archdiscov.services import (
    AnalyzerError,
    AnalyzerRegistry,
    load_snapshot,
    run_analyzer,
    validate_analysis_result,
)
from archdiscov.services.cross_account_trust import CrossAccountTrustAnalyzer
from tests.paths import FIXTURES_ROOT
from tests.support.iam_data import ACCOUNT
from tests.support.trust_data import OTHER, build, role_arn, statement

REGISTRY = AnalyzerRegistry([CrossAccountTrustAnalyzer()])
C = TrustClassification


def analyze(roles, *, iam_status=None, **parameters):
    snapshot = build(roles, iam_status=iam_status)
    result = run_analyzer(
        GraphIndex(snapshot), {"kind": "cross_account_trust", **parameters}, registry=REGISTRY
    )
    assert validate_analysis_result(result, snapshot) == result
    return result


def single(statements, **options):
    result = analyze({"web": statements}, **options)
    [finding] = result.findings
    return finding


def codes(finding):
    return [limitation.code for limitation in finding.limitations]


@pytest.mark.parametrize(
    ("principal", "classification"),
    [
        ({"AWS": OTHER}, C.EXTERNAL_ACCOUNT),
        ({"AWS": f"arn:aws:iam::{OTHER}:root"}, C.EXTERNAL_ACCOUNT),
        ({"AWS": ACCOUNT}, C.SAME_ACCOUNT),
        ({"AWS": f"arn:aws:iam::{ACCOUNT}:role/not-stored"}, C.SAME_ACCOUNT),
        ({"AWS": role_arn("web")}, C.SAME_ACCOUNT),
        ({"Service": "ec2.amazonaws.com"}, C.SERVICE),
        ({"Federated": f"arn:aws:iam::{ACCOUNT}:saml-provider/company"}, C.FEDERATED),
        ({"AWS": "*"}, C.WILDCARD),
        ("*", C.WILDCARD),
        ({"AWS": "arn:aws:iam::*:root"}, C.WILDCARD),
        ({"AWS": "AROAEXAMPLEID"}, C.UNRESOLVED),
        ({"CanonicalUser": "unsupported"}, C.UNRESOLVED),
        ({"AWS": []}, C.UNRESOLVED),
    ],
)
def test_principals_are_classified_from_recorded_edges(principal, classification):
    finding = single([statement(principal)])
    assert finding.trust.classification == classification
    assert finding.trust.source == TrustSource.ROLE_TRUST_POLICY
    assert finding.trust.effect == TrustEffect.ALLOW
    assert finding.trust.conditional is False
    assert "not effective access" in finding.summary
    assert finding.status == AnalysisStatus.SUPPORTED


def test_explicit_cross_account_trust_is_cited_with_its_statement_and_a_terminal_limit():
    finding = single([statement({"AWS": OTHER})])
    principal, trust_statement = finding.evidence_ids
    assert finding.path[0].target.scope.account_id == OTHER
    assert finding.path[0].evidence_ids == finding.evidence_ids
    assert principal != trust_statement
    assert codes(finding) == ["external_target"]
    assert "AROAweb" in finding.resources[0].resource_id


def test_stored_same_account_role_has_no_terminal_limitation():
    finding = single([statement({"AWS": role_arn("web")})])
    assert finding.path[0].target.kind == "local"
    assert finding.limitations == []


def test_conditions_effect_and_not_principal_context_are_kept():
    conditional = single([statement({"AWS": OTHER}, condition=True)])
    assert conditional.trust.conditional is True
    assert "subject to a condition" in conditional.summary
    assert "subject to" not in single([statement({"AWS": OTHER})]).summary
    deny = single([statement({"AWS": OTHER}, effect="Deny")])
    assert deny.trust.effect == TrustEffect.DENY
    assert "Deny" in deny.summary
    negative = single([statement({"AWS": role_arn("web")}, negative=True)])
    assert negative.trust.not_principal is True
    assert negative.trust.classification == C.UNRESOLVED
    assert codes(negative) == ["unresolved_target"]


def test_wildcards_and_unresolved_principals_remain_terminals_with_limitations():
    for principal in ({"AWS": "*"}, "*", {"CanonicalUser": "x"}):
        finding = single([statement(principal)])
        assert codes(finding) == ["unresolved_target"]
        assert finding.path[0].target.kind == "unresolved"
        assert finding.resources[0].resource_id.count("AROAweb") == 1


def test_every_principal_entry_and_statement_gets_its_own_finding():
    result = analyze(
        {
            "web": [
                statement({"AWS": [OTHER, f"arn:aws:iam::{ACCOUNT}:root"]}),
                statement({"Service": "ec2.amazonaws.com"}, effect="Deny", condition=True),
            ],
            "api": [statement({"Federated": "arn:aws:iam::210987654321:saml-provider/x"})],
        }
    )
    assert len(result.findings) == 4
    kinds = sorted(f.trust.classification for f in result.findings)
    assert kinds == sorted([C.EXTERNAL_ACCOUNT, C.SAME_ACCOUNT, C.SERVICE, C.FEDERATED])
    ids = [f.id for f in result.findings]
    assert ids == sorted(ids) and len(set(ids)) == 4


def test_roles_without_trust_principals_produce_no_findings():
    assert analyze({"web": None}).findings == []
    assert analyze({}).findings == []


def test_failed_iam_collection_downgrades_to_partial_with_the_recorded_operation():
    finding = single([statement({"AWS": OTHER})], iam_status="failed")
    assert finding.status == AnalysisStatus.PARTIAL
    assert codes(finding) == ["external_target", "coverage_failed"]
    ok = single([statement({"AWS": OTHER})], iam_status="success")
    assert ok.status == AnalysisStatus.SUPPORTED


def test_statement_without_a_valid_effect_is_insufficient_evidence_not_trust():
    bad = {"Action": "sts:AssumeRole", "Principal": {"AWS": OTHER}}
    finding = single([bad])
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert finding.trust is None
    assert codes(finding)[0] == "trust_statement_unavailable"


def test_result_limit_truncates_deterministically_and_reports_it():
    roles = {"web": [statement({"AWS": [OTHER, "111111111111", "222222222222"]})]}
    full = analyze(roles)
    assert len(full.findings) == 3
    limited = analyze(roles, result_limit=2)
    assert limited.findings == analyze(roles, result_limit=2).findings
    trust = [f for f in limited.findings if f.trust is not None]
    [notice] = [f for f in limited.findings if f.trust is None]
    assert len(trust) == 2
    assert codes(notice) == ["result_limit_reached"]
    assert analyze(roles, result_limit=3).findings == full.findings


def test_azure_returns_an_actionable_unsupported_result():
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    result = run_analyzer(GraphIndex(azure), {"kind": "cross_account_trust"}, registry=REGISTRY)
    assert validate_analysis_result(result, azure) == result
    [finding] = result.findings
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert finding.trust is None and finding.resources == []
    assert codes(finding) == ["unsupported_platform"]
    assert "AWS" in finding.limitations[0].message
    assert "IAM trust policies" in finding.limitations[0].message


def test_invalid_result_limits_are_safe_errors():
    index = GraphIndex(build({}))
    for value in (0, 10_001):
        with pytest.raises(AnalyzerError) as error:
            run_analyzer(
                index, {"kind": "cross_account_trust", "result_limit": value}, registry=REGISTRY
            )
        assert error.value.code == AnalyzerErrorCode.INVALID_PARAMETERS
