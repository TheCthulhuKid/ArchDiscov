import json
from datetime import UTC, datetime

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.parameters import OwnershipParameters
from archdiscov.models.analysis.references import ResourceReference
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.enums import AnalysisStatus, AnalyzerErrorCode, AnalyzerKind
from archdiscov.services import (
    DEFAULT_REGISTRY,
    AnalyzerError,
    AnalyzerRegistry,
    build_result,
    load_snapshot,
    qualify_status,
    run_analyzer,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
OPERATION = "DescribeRouteTables"
EXTRA = {
    "failed": {"failures": [{"code": "UnauthorizedOperation", "message": "denied"}]},
    "incomplete": {"result_count": 1, "reason": "Pagination stopped"},
    "skipped": {"reason": "Not selected"},
    "unsupported": {"reason": "Not supported"},
    "success": {"result_count": 0},
}


def snapshot_with(tmp_path, status):
    data = json.loads((FIXTURES / "partial-coverage.json").read_text())
    for operation in data["coverage"]["operations"]:
        if operation["operation"] == OPERATION:
            for key in ("failures", "result_count", "reason"):
                operation.pop(key, None)
            operation.update(status=status, **EXTRA[status])
    path = tmp_path / f"{status}.json"
    path.write_text(json.dumps(data))
    return load_snapshot(path)


def network_index():
    return GraphIndex(load_snapshot(FIXTURES / "network.json"))


def index_with(tmp_path, status):
    return GraphIndex(snapshot_with(tmp_path, status))


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped", "unsupported"])
def test_limited_coverage_downgrades_supported_with_one_limitation(tmp_path, status):
    index = index_with(tmp_path, status)
    resource = index.resources()[0]
    qualified = qualify_status(index, [resource, resource], AnalysisStatus.SUPPORTED)
    assert qualified.status == AnalysisStatus.PARTIAL
    [limitation] = qualified.limitations
    assert limitation.code == f"coverage_{status}"
    assert limitation.coverage.operation == OPERATION
    assert limitation.coverage.service == "ec2"
    assert limitation.coverage.location == "eu-west-1"


@pytest.mark.parametrize("status", [AnalysisStatus.PARTIAL, AnalysisStatus.INSUFFICIENT_EVIDENCE])
def test_existing_uncertainty_is_never_upgraded(tmp_path, status):
    index = index_with(tmp_path, "failed")
    qualified = qualify_status(index, index.resources(), status)
    assert qualified.status == status
    assert len(qualified.limitations) == 1


def test_successful_zero_and_unmatched_contexts_are_not_limitations(tmp_path):
    index = index_with(tmp_path, "success")
    qualified = qualify_status(index, index.resources(), AnalysisStatus.SUPPORTED)
    assert (qualified.status, qualified.limitations) == (AnalysisStatus.SUPPORTED, [])
    other = network_index()
    qualified = qualify_status(other, other.resources(), AnalysisStatus.SUPPORTED)
    assert (qualified.status, qualified.limitations) == (AnalysisStatus.SUPPORTED, [])


class FakeOwnership:
    kind = AnalyzerKind.OWNERSHIP
    version = "test-1"

    def __init__(self, **overrides):
        self.overrides = overrides
        self.last = None

    def analyze(self, index, parameters):
        findings = []
        for resource in reversed(index.resources()):
            if not index.resource_evidence_ids(resource.id):
                continue  # supported and partial findings need citations
            qualified = qualify_status(index, [resource], AnalysisStatus.SUPPORTED)
            findings.append(
                AnalysisFinding(
                    snapshot_id=index.snapshot_id,
                    summary=f"Synthetic signal for {resource.id}",
                    status=qualified.status,
                    evidence_ids=list(index.resource_evidence_ids(resource.id)),
                    resources=[ResourceReference(resource_id=resource.id, scope=index.scope)],
                    limitations=qualified.limitations,
                )
            )
        values = {
            "snapshot_id": index.snapshot_id,
            "analyzer_version": self.version,
            "parameters": parameters,
        }
        values.update(self.overrides)
        result = build_result(self, index, parameters, findings)
        self.last = result.model_copy(update=values)
        return self.last


def test_run_analyzer_returns_validated_detached_qualified_result(tmp_path):
    snapshot = snapshot_with(tmp_path, "failed")
    index = GraphIndex(snapshot)
    analyzer = FakeOwnership()
    registry = AnalyzerRegistry([analyzer])
    result = run_analyzer(
        index, {"kind": "ownership", "ownership_keys": ["Owner"]}, registry=registry
    )
    assert result.snapshot_id == index.snapshot_id
    assert result.analyzer == AnalyzerKind.OWNERSHIP
    assert result.analyzer_version == "test-1"
    assert result.parameters == OwnershipParameters(ownership_keys=["Owner"])
    [finding] = result.findings
    assert finding.status == AnalysisStatus.PARTIAL
    assert finding.limitations[0].code == "coverage_failed"
    assert validate_analysis_result(result, snapshot) == result
    assert result is not analyzer.last
    result.findings[0].summary = "changed"
    assert analyzer.last.findings[0].summary != "changed"


def test_build_result_orders_findings_and_stamps_time(tmp_path):
    index = network_index()
    analyzer = FakeOwnership()
    result = run_analyzer(index, {"kind": "ownership"}, registry=AnalyzerRegistry([analyzer]))
    ids = [finding.id for finding in result.findings]
    assert ids == sorted(ids)
    assert len(ids) == len([r for r in index.resource_ids if index.resource_evidence_ids(r)]) > 1
    assert result.created_at.tzinfo is not None
    stamped = build_result(
        analyzer, index, OwnershipParameters(), [], created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert stamped.created_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_registry_is_deterministic_and_rejects_unknown_kinds():
    first, second = FakeOwnership(), FakeOwnership()
    second.kind = AnalyzerKind.CROSS_ACCOUNT_TRUST
    registry = AnalyzerRegistry([second, first])
    assert registry.kinds == (AnalyzerKind.CROSS_ACCOUNT_TRUST, AnalyzerKind.OWNERSHIP)
    assert registry.get(AnalyzerKind.OWNERSHIP) is first
    with pytest.raises(ValueError, match="duplicate"):
        AnalyzerRegistry([first, FakeOwnership()])
    with pytest.raises(AnalyzerError) as error:
        registry.get(AnalyzerKind.INTERNET_EXPOSURE)
    assert error.value.code == AnalyzerErrorCode.UNSUPPORTED_ANALYZER
    assert DEFAULT_REGISTRY.kinds == (AnalyzerKind.OWNERSHIP,)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"kind": "nonsense"},
        {"kind": "ownership", "ownership_keys": []},
        {"kind": "ownership", "ownership_keys": ["SECRET-KEY", "SECRET-KEY"]},
    ],
)
def test_invalid_parameters_are_safe_errors(parameters):
    index = network_index()
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(index, parameters, registry=AnalyzerRegistry([FakeOwnership()]))
    assert error.value.code == AnalyzerErrorCode.INVALID_PARAMETERS
    assert "SECRET-KEY" not in str(error.value)


def test_unregistered_default_kind_is_a_safe_error():
    index = network_index()
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(index, {"kind": "cross_account_trust"})
    assert error.value.code == AnalyzerErrorCode.UNSUPPORTED_ANALYZER


@pytest.mark.parametrize(
    "overrides",
    [
        {"snapshot_id": "00000000-0000-4000-8000-0000000000ff"},
        {"analyzer_version": "other"},
        {"parameters": OwnershipParameters(ownership_keys=["Other"])},
    ],
    ids=["snapshot", "version", "parameters"],
)
def test_results_that_misidentify_themselves_are_rejected(overrides):
    index = network_index()
    with pytest.raises(AnalyzerError) as error:
        run_analyzer(
            index, {"kind": "ownership"}, registry=AnalyzerRegistry([FakeOwnership(**overrides)])
        )
    assert error.value.code == AnalyzerErrorCode.INVALID_RESULT
