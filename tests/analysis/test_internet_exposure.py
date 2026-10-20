import pytest

from archdiscov.graph import GraphIndex
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    AwsService,
    ExposureEntryKind,
    NetworkDecision,
)
from archdiscov.services import (
    DEFAULT_REGISTRY,
    AnalyzerError,
    load_snapshot,
    run_analyzer,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT
from tests.support.lb_exposure_data import FULL_COVERAGE, build, ec2_items, responses

D = NetworkDecision
S = AnalysisStatus
K = ExposureEntryKind


def analyze(parameters=None, *, snapshot=None, data=None, items=None, **options):
    snapshot = snapshot or build(data, ec2=items or ec2_items(public_eni=True), **options)
    result = run_analyzer(GraphIndex(snapshot), {"kind": "internet_exposure", **(parameters or {})})
    assert validate_analysis_result(result, snapshot) == result
    return result


def entries(result, kind=None):
    return [
        f
        for f in result.findings
        if f.internet_exposure is not None
        and (kind is None or f.internet_exposure.entry_kind == kind)
    ]


def group(result):
    [finding] = [f for f in result.findings if f.internet_exposure is None]
    return finding


def codes(finding):
    return [item.code for item in finding.limitations]


def public(**changes):
    data = responses()
    data["describe_load_balancers"]["LoadBalancers"][0].update(Scheme="internet-facing", **changes)
    return data


# ---- findings -----------------------------------------------------------------------


def test_both_entry_kinds_are_reported_as_supported_candidate_exposure():
    result = analyze(coverage=FULL_COVERAGE)
    assert {f.internet_exposure.entry_kind for f in result.findings} == set(K)
    assert [f.id for f in result.findings] == sorted(f.id for f in result.findings)
    for finding in result.findings:
        assert finding.status == S.SUPPORTED
        assert finding.internet_exposure.decision == D.ALLOW
        assert "not proof of reachability" in finding.summary
        assert finding.evidence_ids
    assert result.findings and not [f for f in result.findings if f.internet_exposure is None]


def test_a_load_balancer_finding_names_the_entry_then_its_backend_and_the_observed_health():
    [finding] = entries(analyze(coverage=FULL_COVERAGE), K.LOAD_BALANCER)
    assert len(finding.resources) == 2
    assert finding.resources[1].resource_id.endswith("i-web")
    assert [h.value for h in finding.internet_exposure.observed_target_health] == ["unhealthy"]
    assert finding.internet_exposure.decision == D.ALLOW


def test_denied_and_unknown_candidates_are_reported_with_the_matching_status():
    result = analyze(coverage=FULL_COVERAGE, data=responses())  # an internal balancer
    [balancer] = entries(result, K.LOAD_BALANCER)
    assert balancer.internet_exposure.decision == D.DENY
    assert balancer.status == S.SUPPORTED
    items = ec2_items(public_eni=True)
    del items[Op.DESCRIBE_ROUTE_TABLES]
    unknown = analyze(items=items, coverage=FULL_COVERAGE)
    assert {f.status for f in entries(unknown)} == {S.INSUFFICIENT_EVIDENCE}
    assert all(f.limitations for f in entries(unknown))


def test_max_depth_controls_whether_backends_are_followed():
    items = ec2_items(public_eni=True, backend_rules=[])
    shallow = analyze({"max_depth": 1}, items=items, coverage=FULL_COVERAGE)
    deep = analyze({"max_depth": 2}, items=items, coverage=FULL_COVERAGE)
    assert entries(shallow, K.LOAD_BALANCER)[0].internet_exposure.decision == D.ALLOW
    assert entries(deep, K.LOAD_BALANCER)[0].internet_exposure.decision == D.DENY
    assert len(entries(shallow, K.LOAD_BALANCER)[0].resources) == 1


# ---- collection coverage ------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
@pytest.mark.parametrize(
    "operation", ["DescribeInstances", "DescribeNetworkInterfaces", "DescribeInternetGateways"]
)
def test_limited_ec2_collection_downgrades_findings_and_is_reported(operation, status):
    result = analyze(coverage={**FULL_COVERAGE, (AwsService.EC2, operation): status})
    assert f"coverage_{status}" in codes(group(result))
    assert all(f.status != S.SUPPORTED for f in entries(result, K.PUBLIC_ADDRESS))


def test_limited_routing_collection_makes_exposure_unknown():
    coverage = {**FULL_COVERAGE, (AwsService.EC2, "DescribeRouteTables"): "failed"}
    result = analyze(coverage=coverage)
    assert {f.internet_exposure.decision for f in entries(result)} == {D.UNKNOWN}
    assert all("coverage_limited" in codes(f) for f in entries(result))


@pytest.mark.parametrize("operation", ["DescribeLoadBalancers", "DescribeListeners"])
def test_limited_load_balancer_collection_makes_the_balancer_unknown_and_is_reported(operation):
    result = analyze(coverage=FULL_COVERAGE, limited=operation)
    [balancer] = entries(result, K.LOAD_BALANCER)
    assert balancer.internet_exposure.decision == D.UNKNOWN
    assert "coverage_failed" in codes(group(result))
    assert entries(result, K.PUBLIC_ADDRESS)[0].internet_exposure.decision == D.ALLOW


def test_unrecorded_services_are_reported_without_invented_operations():
    result = analyze()  # only the load-balancing collection is recorded
    assert codes(group(result)) == ["coverage_unrecorded_ec2"]
    [limitation] = group(result).limitations
    assert limitation.coverage is None
    assert group(result).status == S.INSUFFICIENT_EVIDENCE


# ---- bounds and ordering ------------------------------------------------------------


def test_the_result_limit_keeps_the_most_informative_candidates_first():
    items = ec2_items(public_eni=True)
    result = analyze({"result_limit": 1}, items=items, coverage=FULL_COVERAGE)
    assert len(entries(result)) == 1
    assert "result_limit_reached" in codes(group(result))
    assert entries(result)[0].internet_exposure.decision == D.ALLOW
    full = analyze(items=items, coverage=FULL_COVERAGE)
    assert len(entries(full)) == 2 and not [f for f in full.findings if f.internet_exposure is None]


def test_candidates_are_ordered_by_decision_before_the_limit_applies():
    data = responses()  # internal: denied, so the public interface is kept first
    result = analyze({"result_limit": 1}, data=data, coverage=FULL_COVERAGE)
    [kept] = entries(result)
    assert kept.internet_exposure.entry_kind == K.PUBLIC_ADDRESS


def test_results_are_deterministic():
    first, second = analyze(coverage=FULL_COVERAGE), analyze(coverage=FULL_COVERAGE)
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert first.parameters.max_depth == 3 and first.parameters.result_limit == 100


# ---- platform and parameters --------------------------------------------------------


def test_azure_returns_an_actionable_unsupported_result():
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    result = run_analyzer(GraphIndex(azure), {"kind": "internet_exposure"})
    [finding] = result.findings
    assert finding.status == S.INSUFFICIENT_EVIDENCE
    assert finding.internet_exposure is None
    assert finding.limitations[0].code == "unsupported_platform"
    assert "internet exposure" in finding.limitations[0].message


def test_a_snapshot_without_entry_points_has_no_exposure_findings():
    items = ec2_items()
    snapshot = build(public(), ec2=items, coverage=FULL_COVERAGE)
    assert [f.internet_exposure.entry_kind for f in entries(analyze(snapshot=snapshot))] == [
        K.LOAD_BALANCER
    ]


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_depth": 0},
        {"max_depth": 11},
        {"result_limit": 0},
        {"result_limit": 10001},
        {"resource_id": "x"},
        {"port": 80},
    ],
)
def test_invalid_parameters_are_safe_errors(parameters):
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(GraphIndex(build()), {"kind": "internet_exposure", **parameters})
    assert error.value.code == AnalyzerErrorCode.INVALID_PARAMETERS


def test_the_analyzer_is_registered():
    assert "internet_exposure" in [kind.value for kind in DEFAULT_REGISTRY.kinds]
