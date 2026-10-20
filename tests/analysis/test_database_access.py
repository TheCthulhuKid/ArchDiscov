import re
from urllib.parse import unquote

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    AnalyzerKind,
    AwsService,
    NetworkDecision,
)
from archdiscov.services import (
    AnalyzerError,
    load_snapshot,
    run_analyzer,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT
from tests.support.rds_data import CLUSTER, INSTANCE
from tests.support.source_data import FULL_COVERAGE, build, ec2_items, rid, rule

D = NetworkDecision
S = AnalysisStatus
RDS_ID = rid(INSTANCE, AwsService.RDS)
EGRESS_ALL = rule("sg-app", egress=True, protocol="-1", cidr="0.0.0.0/0")
DB_EGRESS = rule("sg-a", egress=True, protocol="-1", cidr="0.0.0.0/0")


def analyze(parameters=None, *, snapshot=None, **options):
    snapshot = snapshot or build(**{"task": {}, "function": {}, **options})
    index = GraphIndex(snapshot)
    result = run_analyzer(
        index,
        {"kind": "database_access", "resource_id": RDS_ID, "port": 5432, **(parameters or {})},
    )
    assert validate_analysis_result(result, snapshot) == result
    return result


def sources(result):
    return {
        re.split(r"[:/]", unquote(f.resources[0].resource_id))[-1]: f
        for f in result.findings
        if f.database_access is not None
    }


def group(result):
    [finding] = [f for f in result.findings if f.database_access is None]
    return finding


def codes(finding):
    return [limitation.code for limitation in finding.limitations]


# ---- findings -----------------------------------------------------------------------


def test_allowed_sources_are_supported_findings_naming_the_source_and_database():
    result = analyze(coverage=FULL_COVERAGE)
    found = sources(result)
    finding = found["i-plain"]
    assert finding.status == S.SUPPORTED
    assert finding.database_access.decision == D.ALLOW
    assert [r.resource_id for r in finding.resources] == [rid("i-plain"), RDS_ID]
    assert finding.evidence_ids
    assert "not authentication" in finding.summary
    assert {"ec2_instance", "ecs_task", "lambda_function"} == {
        f.database_access.source_kind for f in found.values()
    }
    assert all("effective" not in f.summary for f in found.values())


def test_denied_sources_are_supported_conclusions_with_cited_blockers():
    rules = [EGRESS_ALL, DB_EGRESS]  # no ingress rule on the database
    result = analyze(ec2=ec2_items(rules=rules), coverage=FULL_COVERAGE)
    finding = sources(result)["i-plain"]
    assert finding.database_access.decision == D.DENY
    assert finding.status == S.SUPPORTED
    assert any(item.code == "denied" and item.evidence_ids for item in finding.limitations)


def test_unknown_sources_are_insufficient_evidence_never_supported():
    finding = sources(analyze(coverage=FULL_COVERAGE))["i-nosg"]
    assert finding.database_access.decision == D.UNKNOWN
    assert finding.status == S.INSUFFICIENT_EVIDENCE
    assert finding.limitations


def test_a_complete_collection_has_no_group_finding_only_the_other_vpc_note():
    result = analyze(coverage=FULL_COVERAGE)
    assert codes(group(result)) == ["sources_other_vpc"]


def test_the_database_cluster_can_be_selected_too():
    result = analyze({"resource_id": rid(CLUSTER, AwsService.RDS)}, coverage=FULL_COVERAGE)
    assert sources(result)["i-plain"].database_access.decision == D.ALLOW


# ---- collection coverage ------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "operation", "source"),
    [
        (AwsService.EC2, "DescribeInstances", "i-plain"),
        (AwsService.ECS, "DescribeTasks", "0123456789abcdef"),
        (AwsService.LAMBDA, "GetFunctionConfiguration", "worker"),
    ],
)
@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
def test_limited_source_collection_downgrades_findings_to_partial_and_is_reported(
    service, operation, source, status
):
    result = analyze(coverage={**FULL_COVERAGE, (service, operation): status})
    assert sources(result)[source].status == S.PARTIAL
    assert f"coverage_{status}" in codes(sources(result)[source])
    assert f"coverage_{status}" in codes(group(result))
    other = next(k for k in ("i-plain", "worker", "0123456789abcdef") if k != source)
    assert sources(result)[other].status in {S.SUPPORTED, S.PARTIAL}


def test_limited_rds_collection_downgrades_every_finding():
    result = analyze(coverage=FULL_COVERAGE, rds_status="failed")
    found = sources(result)
    assert all(
        f.status != S.SUPPORTED for f in found.values() if f.database_access.decision != D.UNKNOWN
    )
    assert "coverage_failed" in codes(group(result))


def test_limited_nacl_or_route_collection_makes_the_path_unknown():
    for operation in ("DescribeNetworkAcls", "DescribeRouteTables"):
        coverage = {**FULL_COVERAGE, (AwsService.EC2, operation): "failed"}
        finding = sources(analyze(coverage=coverage))["worker"]
        assert finding.database_access.decision == D.UNKNOWN
        assert "coverage_limited" in codes(finding)


def test_unrecorded_services_are_reported_without_invented_operations():
    result = analyze()  # only RDS records its coverage
    unrecorded = [c for c in codes(group(result)) if c.startswith("coverage_unrecorded_")]
    assert unrecorded == [
        "coverage_unrecorded_ec2",
        "coverage_unrecorded_ecs",
        "coverage_unrecorded_lambda",
    ]
    assert all(x.coverage is None for x in group(result).limitations if "unrecorded" in x.code)
    assert group(result).status == S.INSUFFICIENT_EVIDENCE


def test_placement_gaps_are_reported_at_group_level():
    result = analyze({"port": 3306}, coverage=FULL_COVERAGE)
    assert "database_port_mismatch" in codes(group(result))
    assert group(result).resources[0].resource_id == RDS_ID


# ---- bounds and ordering ------------------------------------------------------------


def test_the_result_limit_keeps_decided_sources_first_and_reports_truncation():
    result = analyze({"result_limit": 3}, coverage=FULL_COVERAGE)
    kept = sources(result)
    assert len(kept) == 3
    assert {f.database_access.decision for f in kept.values()} == {D.ALLOW}
    assert "result_limit_reached" in codes(group(result))
    full = analyze(coverage=FULL_COVERAGE)
    assert len(sources(full)) == 5
    assert "result_limit_reached" not in codes(group(full))


def test_results_are_deterministic_and_ordered_by_finding_identity():
    first, second = analyze(coverage=FULL_COVERAGE), analyze(coverage=FULL_COVERAGE)
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert [f.id for f in first.findings] == sorted(f.id for f in first.findings)
    assert first.parameters.result_limit == 100


# ---- selection and platform ---------------------------------------------------------


def test_azure_returns_an_actionable_unsupported_result():
    azure = load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json")
    index = GraphIndex(azure)
    resource = index.resource_ids[0]
    result = run_analyzer(index, {"kind": "database_access", "resource_id": resource, "port": 5432})
    [finding] = result.findings
    assert finding.status == S.INSUFFICIENT_EVIDENCE
    assert finding.database_access is None
    assert finding.limitations[0].code == "unsupported_platform"
    assert "database access" in finding.limitations[0].message
    assert finding.resources[0].resource_id == resource


@pytest.mark.parametrize(
    ("resource", "code"),
    [
        ("absent", AnalyzerErrorCode.RESOURCE_NOT_FOUND),
        (rid("subnet-a"), AnalyzerErrorCode.UNSUPPORTED_RESOURCE),
    ],
)
def test_unknown_and_wrongly_typed_selections_are_safe_errors(resource, code):
    snapshot = build()
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(
            GraphIndex(snapshot),
            {"kind": "database_access", "resource_id": resource, "port": 5432},
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "parameters",
    [
        {"kind": "database_access", "resource_id": RDS_ID},
        {"kind": "database_access", "resource_id": RDS_ID, "port": 0},
        {"kind": "database_access", "resource_id": RDS_ID, "port": 5432, "result_limit": 0},
        {"kind": "database_access", "port": 5432},
        {"kind": "database_access", "resource_id": RDS_ID, "port": 5432, "max_depth": 2},
    ],
)
def test_invalid_parameters_are_safe_errors(parameters):
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(GraphIndex(build()), parameters)
    assert error.value.code == AnalyzerErrorCode.INVALID_PARAMETERS


def test_the_analyzer_is_registered_with_a_stable_kind():
    from archdiscov.services import DEFAULT_REGISTRY  # noqa: PLC0415

    assert AnalyzerKind.DATABASE_ACCESS in DEFAULT_REGISTRY.kinds
