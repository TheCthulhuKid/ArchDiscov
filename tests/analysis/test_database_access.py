import re
from urllib.parse import unquote

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalysisStatus, AwsService, NetworkDecision
from archdiscov.services import load_snapshot, run_analyzer, validate_analysis_result
from tests.paths import FIXTURES_ROOT
from tests.support.rds_data import INSTANCE
from tests.support.source_data import FULL_COVERAGE, build, ec2_items, rid, rule

D = NetworkDecision
S = AnalysisStatus
RDS_ID = rid(INSTANCE, AwsService.RDS)
EGRESS_ALL = rule("sg-app", egress=True, protocol="-1", cidr="0.0.0.0/0")
DB_EGRESS = rule("sg-a", egress=True, protocol="-1", cidr="0.0.0.0/0")


def analyze(parameters=None, **options):
    snapshot = build(**{"task": {}, "function": {}, **options})
    result = run_analyzer(
        GraphIndex(snapshot),
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


def test_unknown_sources_are_insufficient_and_other_vpcs_are_only_noted():
    result = analyze(coverage=FULL_COVERAGE)
    finding = sources(result)["i-nosg"]
    assert finding.database_access.decision == D.UNKNOWN
    assert finding.status == S.INSUFFICIENT_EVIDENCE
    assert finding.limitations
    assert codes(group(result)) == ["sources_other_vpc"]


# ---- collection coverage ------------------------------------------------------------


def test_limited_source_or_database_collection_downgrades_findings_and_is_reported():
    coverage = {**FULL_COVERAGE, (AwsService.EC2, "DescribeInstances"): "failed"}
    result = analyze(coverage=coverage)
    assert sources(result)["i-plain"].status == S.PARTIAL
    assert "coverage_failed" in codes(sources(result)["i-plain"])
    assert "coverage_failed" in codes(group(result))
    assert sources(result)["worker"].status == S.SUPPORTED
    # The database is on every path, so its limited collection downgrades every decision.
    result = analyze(coverage=FULL_COVERAGE, rds_status="failed")
    decided = [f for f in sources(result).values() if f.database_access.decision != D.UNKNOWN]
    assert decided and all(f.status != S.SUPPORTED for f in decided)
    assert "coverage_failed" in codes(group(result))


def test_limited_nacl_collection_makes_the_path_unknown():
    coverage = {**FULL_COVERAGE, (AwsService.EC2, "DescribeNetworkAcls"): "failed"}
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


# ---- bounds and platform ------------------------------------------------------------


def test_the_result_limit_keeps_decided_sources_first_and_reports_truncation():
    result = analyze({"result_limit": 3}, coverage=FULL_COVERAGE)
    kept = sources(result)
    assert len(kept) == 3
    assert {f.database_access.decision for f in kept.values()} == {D.ALLOW}
    assert "result_limit_reached" in codes(group(result))
    full = analyze(coverage=FULL_COVERAGE)
    assert len(sources(full)) == 5
    assert "result_limit_reached" not in codes(group(full))


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
