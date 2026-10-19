import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    ImpactKind,
    ImpactLabel,
    RuleDirection,
    RuleReferenceSide,
)
from archdiscov.services import (
    AnalyzerError,
    AnalyzerRegistry,
    load_snapshot,
    run_analyzer,
    upgrade_snapshot,
    validate_analysis_result,
    validate_snapshot,
)
from archdiscov.services.security_group_impact import SecurityGroupImpactAnalyzer
from tests.paths import FIXTURES_ROOT
from tests.support.security_group_data import EVIDENCE, IDS, OTHER_ACCOUNT, variant

REGISTRY = AnalyzerRegistry([SecurityGroupImpactAnalyzer()])
ATTACHERS = ["instance", "eni", "lb", "ecs", "lambda", "rds"]


def analyze(data=None, group="sg-app", **parameters):
    snapshot = validate_snapshot(data or variant())
    resource_id = IDS.get(group, group)
    result = run_analyzer(
        GraphIndex(snapshot),
        {"kind": "security_group_impact", "resource_id": resource_id, **parameters},
        registry=REGISTRY,
    )
    assert validate_analysis_result(result, snapshot) == result
    return result


def by_kind(result, kind):
    return [f for f in result.findings if f.impact is not None and f.impact.kind == kind]


def group_finding(result):
    return next((f for f in result.findings if f.impact is None), None)


def codes(finding):
    return [limitation.code for limitation in finding.limitations]


def test_every_attaching_service_is_a_cited_observed_direct_attachment():
    result = analyze()
    direct = by_kind(result, ImpactKind.DIRECT_ATTACHMENT)
    assert {f.resources[0].resource_id for f in direct} == {IDS[name] for name in ATTACHERS}
    for finding in direct:
        name = next(n for n in ATTACHERS if IDS[n] == finding.resources[0].resource_id)
        assert finding.impact.label == ImpactLabel.OBSERVED
        assert finding.impact.selected_group_id == IDS["sg-app"]
        assert finding.status == AnalysisStatus.SUPPORTED
        assert finding.evidence_ids == [EVIDENCE[name]]
        [step] = finding.path
        assert (step.source_id, step.target.resource_id) == (IDS[name], IDS["sg-app"])
        assert step.attributes == {"kind": "security_group_membership"}
    other = {f.resources[0].resource_id for f in direct}
    assert IDS["contained"] not in other and IDS["subnet"] not in other


def test_unattached_group_and_containment_only_placement_have_no_attachments():
    result = analyze(group="sg-lonely")
    assert result.findings == []
    # Lambda attaches to a different group; the selected group does not inherit that.
    db = analyze(group="sg-db")
    attached = {f.resources[0].resource_id for f in by_kind(db, ImpactKind.DIRECT_ATTACHMENT)}
    assert attached == {IDS["lambda"]}


@pytest.mark.parametrize(
    ("group", "code"),
    [
        ("absent", AnalyzerErrorCode.RESOURCE_NOT_FOUND),
        ("instance", AnalyzerErrorCode.UNSUPPORTED_RESOURCE),
    ],
)
def test_missing_and_non_group_selections_are_safe_errors(group, code):
    with pytest.raises(AnalyzerError) as error:
        analyze(group=group)
    assert error.value.code == code


def test_failed_attaching_service_coverage_downgrades_and_is_reported():
    result = analyze(variant(statuses={"lambda": "failed"}))
    lambda_finding = next(
        f
        for f in by_kind(result, ImpactKind.DIRECT_ATTACHMENT)
        if f.resources[0].resource_id == IDS["lambda"]
    )
    assert lambda_finding.status == AnalysisStatus.PARTIAL
    assert codes(lambda_finding) == ["coverage_failed"]
    assert "coverage_failed" in codes(group_finding(result))
    untouched = [
        f for f in by_kind(result, ImpactKind.DIRECT_ATTACHMENT) if f is not lambda_finding
    ]
    assert all(f.status == AnalysisStatus.SUPPORTED for f in untouched)


@pytest.mark.parametrize("missing", ["ec2", "ecs", "rds"])
def test_unrecorded_attaching_service_coverage_is_named_without_inventing_operations(missing):
    services = tuple(
        s for s in ("ec2", "elasticloadbalancing", "ecs", "lambda", "rds") if s != missing
    )
    result = analyze(variant(services=services), group="sg-lonely")
    finding = group_finding(result)
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert codes(finding) == [f"coverage_unrecorded_{missing}"]
    assert all(limitation.coverage is None for limitation in finding.limitations)


def test_complete_coverage_leaves_no_group_level_finding_for_an_unattached_group():
    assert group_finding(analyze(group="sg-lonely")) is None


def test_rule_references_cover_same_account_both_sides_and_directions():
    result = analyze()
    rules = by_kind(result, ImpactKind.RULE_REFERENCE)
    seen = {
        (f.impact.reference_side, f.impact.rule_direction, f.resources[0].resource_id)
        for f in rules
        if f.resources
    }
    assert (RuleReferenceSide.SELECTED_REFERENCES_OTHER, RuleDirection.EGRESS, IDS["sg-db"]) in seen
    assert (
        RuleReferenceSide.OTHER_REFERENCES_SELECTED,
        RuleDirection.INGRESS,
        IDS["sg-cache"],
    ) in seen
    egress = next(f for f in rules if f.resources and f.resources[0].resource_id == IDS["sg-db"])
    assert egress.impact.label == ImpactLabel.OBSERVED
    assert egress.path[0].attributes["from_port"] == 5432
    assert egress.path[0].attributes["protocol"] == "tcp"
    assert egress.evidence_ids == [EVIDENCE["sg-app"]]


def test_self_reference_is_reported_once_as_the_group_referencing_itself():
    group = IDS["sg-app"]
    selfish = [
        f
        for f in by_kind(analyze(), ImpactKind.RULE_REFERENCE)
        if f.path[0].source_id == group and getattr(f.path[0].target, "resource_id", None) == group
    ]
    assert len(selfish) == 1
    assert selfish[0].impact.reference_side == RuleReferenceSide.SELECTED_REFERENCES_OTHER
    assert selfish[0].resources[0].resource_id == group


def test_cross_account_and_unresolved_references_stay_terminals_with_limitations():
    index = GraphIndex(validate_snapshot(variant()))
    rules = by_kind(analyze(), ImpactKind.RULE_REFERENCE)
    external = next(f for f in rules if f.path[0].target.kind == "external")
    assert external.resources == []
    assert external.path[0].target.scope.account_id == OTHER_ACCOUNT
    assert codes(external) == ["external_target"]
    assert index.get_resource(external.path[0].target.identifier) is None
    unresolved = next(f for f in rules if f.path[0].target.kind == "unresolved")
    assert unresolved.resources == []
    assert codes(unresolved) == ["unresolved_target"]
    assert unresolved.limitations[0].unresolved_target.target.source_path == "Rules[9]"
    assert not any(getattr(f.path[0].target, "identifier", None) == "pl-123" for f in rules), (
        "prefix lists are not security groups"
    )


def test_reference_without_a_recorded_direction_becomes_a_group_limitation():
    result = analyze()
    assert "rule_direction_unrecorded" in codes(group_finding(result))
    assert not any(
        f.path
        and f.path[0].source_id == IDS["sg-db"]
        and f.impact.kind == ImpactKind.RULE_REFERENCE
        for f in by_kind(result, ImpactKind.RULE_REFERENCE)
    )


def test_failed_rule_collection_downgrades_rule_findings():
    result = analyze(variant(statuses={"ec2": "failed"}))
    rules = by_kind(result, ImpactKind.RULE_REFERENCE)
    assert rules and all(f.status == AnalysisStatus.PARTIAL for f in rules)
    assert all("coverage_failed" in codes(f) for f in rules)


def test_result_limit_truncates_deterministically_and_reports_it():
    full = analyze()
    impact_findings = [f for f in full.findings if f.impact is not None]
    assert len(impact_findings) > 3
    limited = analyze(result_limit=3)
    again = analyze(result_limit=3)
    assert limited.findings == again.findings
    assert len([f for f in limited.findings if f.impact is not None]) == 3
    assert "result_limit_reached" in codes(group_finding(limited))
    assert "result_limit_reached" not in codes(group_finding(full))
    exact = analyze(result_limit=len(impact_findings))
    assert "result_limit_reached" not in codes(group_finding(exact))


def test_unsupported_platform_returns_an_actionable_unsupported_result():
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    index = GraphIndex(azure)
    selected = index.resource_ids[0]
    result = run_analyzer(
        index,
        {"kind": "security_group_impact", "resource_id": selected},
        registry=REGISTRY,
    )
    assert validate_analysis_result(result, azure) == result
    [finding] = result.findings
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert finding.impact is None
    assert codes(finding) == ["unsupported_platform"]
    assert "AWS" in finding.limitations[0].message
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(
            index,
            {"kind": "security_group_impact", "resource_id": "absent"},
            registry=REGISTRY,
        )
    assert error.value.code == AnalyzerErrorCode.RESOURCE_NOT_FOUND


def test_schema_two_and_three_snapshots_give_identical_findings():
    legacy = validate_snapshot(variant())
    modern = upgrade_snapshot(legacy)
    parameters = {"kind": "security_group_impact", "resource_id": IDS["sg-app"]}
    results = [
        run_analyzer(GraphIndex(snapshot), parameters, registry=REGISTRY)
        for snapshot in (legacy, modern)
    ]
    for result, snapshot in zip(results, (legacy, modern), strict=True):
        assert validate_analysis_result(result, snapshot) == result
    assert [f.id for f in results[0].findings] == [f.id for f in results[1].findings]
