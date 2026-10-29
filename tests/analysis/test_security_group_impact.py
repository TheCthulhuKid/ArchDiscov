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
    validate_analysis_result,
    validate_snapshot,
)
from archdiscov.services import security_group_downstream as downstream_module
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


def potential(result):
    return by_kind(result, ImpactKind.DOWNSTREAM_DEPENDENCY)


def chain(finding):
    return [
        (step.source_id, getattr(step.target, "resource_id", step.target.kind))
        for step in finding.path
    ]


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
    assert analyze(group="sg-lonely").findings == []
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


def test_failed_rule_collection_downgrades_rule_and_traced_findings():
    result = analyze(variant(statuses={"ec2": "failed"}))
    rules = by_kind(result, ImpactKind.RULE_REFERENCE)
    assert rules and all(f.status == AnalysisStatus.PARTIAL for f in rules)
    assert all("coverage_failed" in codes(f) for f in rules)
    traced = [f for f in potential(result) if f.resources]
    assert traced and all(f.status == AnalysisStatus.PARTIAL for f in traced)


def test_unrecorded_attaching_service_coverage_is_named_without_inventing_operations():
    services = ("ec2", "elasticloadbalancing", "ecs", "lambda")
    finding = group_finding(analyze(variant(services=services), group="sg-lonely"))
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert codes(finding) == ["coverage_unrecorded_rds"]
    assert all(limitation.coverage is None for limitation in finding.limitations)


def test_rule_references_cover_both_sides_directions_and_self_reference():
    rules = by_kind(analyze(), ImpactKind.RULE_REFERENCE)
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
    group = IDS["sg-app"]
    [selfish] = [
        f
        for f in rules
        if f.path[0].source_id == group and getattr(f.path[0].target, "resource_id", None) == group
    ]
    assert selfish.impact.reference_side == RuleReferenceSide.SELECTED_REFERENCES_OTHER
    assert selfish.resources[0].resource_id == group


def test_cross_account_unresolved_and_undirected_references_are_limitations():
    index = GraphIndex(validate_snapshot(variant()))
    result = analyze()
    rules = by_kind(result, ImpactKind.RULE_REFERENCE)
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
    # A reference without a recorded direction is a group limitation, not a finding.
    assert "rule_direction_unrecorded" in codes(group_finding(result))
    assert not any(f.path and f.path[0].source_id == IDS["sg-db"] for f in rules)


def test_result_limit_truncates_and_reports_it():
    full = analyze()
    impact_findings = [f for f in full.findings if f.impact is not None]
    assert len(impact_findings) > 3
    limited = analyze(result_limit=3)
    assert len([f for f in limited.findings if f.impact is not None]) == 3
    assert "result_limit_reached" in codes(group_finding(limited))
    assert "result_limit_reached" not in codes(group_finding(full))
    exact = analyze(result_limit=len(impact_findings))
    assert "result_limit_reached" not in codes(group_finding(exact))


def test_unsupported_platform_returns_an_actionable_unsupported_result():
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    index = GraphIndex(azure)
    result = run_analyzer(
        index,
        {"kind": "security_group_impact", "resource_id": index.resource_ids[0]},
        registry=REGISTRY,
    )
    assert validate_analysis_result(result, azure) == result
    [finding] = result.findings
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert finding.impact is None
    assert codes(finding) == ["unsupported_platform"]
    assert "AWS" in finding.limitations[0].message


def test_downstream_chains_are_potential_cited_and_separate_from_observed():
    result = analyze()
    downstream = potential(result)
    assert downstream
    assert all(f.impact.label == ImpactLabel.POTENTIAL for f in downstream)
    observed = [f for f in result.findings if f.impact and f.impact.label == ImpactLabel.OBSERVED]
    assert observed and not {f.id for f in observed} & {f.id for f in downstream}
    one = next(
        f
        for f in downstream
        if chain(f) == [(IDS["instance"], IDS["subnet"])] and not f.path[0].attributes.get("note")
    )
    assert one.resources[0].resource_id == IDS["subnet"]
    assert one.evidence_ids == [EVIDENCE["instance"]]
    assert "not reachability" in one.summary
    two = [
        f
        for f in downstream
        if chain(f) == [(IDS["instance"], IDS["subnet"]), (IDS["subnet"], IDS["vpc"])]
    ]
    assert two and all(f.resources[0].resource_id == IDS["vpc"] for f in two)


def test_tracing_stops_at_cycles_the_selected_group_and_other_accounts():
    downstream = potential(analyze(max_depth=10))
    steps = [step for f in downstream for step in f.path]
    assert not any(
        step.source_id == IDS["vpc"] and step.target.resource_id == IDS["subnet"]
        for step in steps
        if step.target.kind == "local"
    )
    assert all(getattr(step.target, "resource_id", None) != IDS["sg-app"] for step in steps)
    ended = next(f for f in downstream if f.path[-1].target.kind == "external")
    assert ended.resources == []
    assert ended.path[-1].target.scope.account_id == OTHER_ACCOUNT
    assert codes(ended) == ["external_target"]
    assert len(ended.path) == 1


def test_depth_boundaries_report_truncation_only_when_structure_remains():
    shallow = analyze(max_depth=1)
    assert {len(f.path) for f in potential(shallow)} == {1}
    assert "depth_limit_reached" in codes(group_finding(shallow))
    exact = analyze(max_depth=2)
    assert max(len(f.path) for f in potential(exact)) == 2
    assert "depth_limit_reached" not in codes(group_finding(exact))


def test_work_budget_truncation_is_reported(monkeypatch):
    monkeypatch.setattr(downstream_module, "WORK_BUDGET", 1)
    assert "work_limit_reached" in codes(group_finding(analyze()))
