"""Audit the phase 3 completion criteria across every analyzer and comparison.

The audit is driven from the public enums: it fails for an analyzer kind or a comparison
source kind that has no scenario, so new kinds cannot ship unaudited.
"""

from functools import cache

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalysisStatus, AnalyzerKind
from archdiscov.services import (
    DEFAULT_REGISTRY,
    AnalysisValidationError,
    SnapshotConflictError,
    load_analysis,
    run_analyzer,
    save_analysis,
    search_resources,
)
from tests.paths import FIXTURES_ROOT
from tests.support.audit import finding_violations, result_violations
from tests.support.audit_scenarios import (
    LIMITED_STATUSES,
    SCENARIOS,
    SCHEMAS,
    as_schema,
    with_coverage,
)
from tests.support.tagged_snapshots import NO_TAGS, tagged


@cache
def snapshot(kind, schema, coverage="success"):
    status = None if coverage == "unrecorded" else coverage
    return with_coverage(as_schema(SCENARIOS[kind].build(), schema), status)


@cache
def analysis(kind, schema, coverage="success"):
    return run_analyzer(
        GraphIndex(snapshot(kind, schema, coverage)),
        {"kind": kind, **SCENARIOS[kind].parameters},
    )


KINDS = list(AnalyzerKind)
# Schemas 1 and 2 are audited with full coverage; the other checks use the current schema.
CURRENT_SCHEMA = SCHEMAS[-1]
TAGS = {"Name": "app"}


def test_every_analyzer_kind_has_a_scenario_and_is_registered():
    assert set(SCENARIOS) == set(AnalyzerKind)
    for kind in AnalyzerKind:
        assert DEFAULT_REGISTRY.get(kind).kind == kind


# ---- findings cite, qualify, and follow coverage ----------------------------------


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("kind", KINDS)
def test_findings_with_full_coverage_cite_evidence_and_paths(kind, schema):
    result = analysis(kind, schema)
    assert result.findings
    assert result_violations(result) == []
    assert any(f.status == AnalysisStatus.SUPPORTED for f in result.findings)


@pytest.mark.parametrize("status", LIMITED_STATUSES)
@pytest.mark.parametrize("kind", KINDS)
def test_limited_coverage_never_produces_an_unqualified_conclusion(kind, status):
    result = analysis(kind, CURRENT_SCHEMA, status)
    assert result.findings
    assert result_violations(result, limited=True) == []


@pytest.mark.parametrize("kind", KINDS)
def test_unrecorded_coverage_is_reported_without_inventing_operations(kind):
    result = analysis(kind, CURRENT_SCHEMA, "unrecorded")
    assert result_violations(result, unrecorded=True) == []
    if kind == AnalyzerKind.OWNERSHIP:  # absent records are not interpreted
        assert result.findings == analysis(kind, CURRENT_SCHEMA, "success").findings


def test_unavailable_tag_data_is_never_an_ownership_conclusion():
    variants = [(NO_TAGS, None), (TAGS, None), (TAGS, "omitted"), (TAGS, "unresolved")]
    for tags, cite in variants:
        source, _ = tagged("analysis/ownership-snapshot.json", tags=tags, cite=cite)
        result = run_analyzer(GraphIndex(source), {"kind": AnalyzerKind.OWNERSHIP})
        assert result.findings
        assert all(f.status == AnalysisStatus.INSUFFICIENT_EVIDENCE for f in result.findings)
        assert result_violations(result) == []
        assert {limitation.code for f in result.findings for limitation in f.limitations} >= {
            "tags_unavailable"
        }


def test_external_unresolved_and_unknown_elements_lower_conclusions():
    terminals, unknown = set(), 0
    for kind in KINDS:
        for finding in analysis(kind, "2").findings:
            terminals |= {item.code for item in finding.limitations} & {
                "external_target",
                "unresolved_target",
            }
            block = finding.database_access or finding.internet_exposure
            if block is not None and block.decision == "unknown":
                unknown += 1
                assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert terminals == {"external_target", "unresolved_target"}
    assert unknown


# ---- the audit itself catches regressions -----------------------------------------


def test_the_audit_fails_when_a_finding_loses_its_citations_or_qualification():
    result = analysis(AnalyzerKind.SECURITY_GROUP_IMPACT, "2")
    concluded = next(f for f in result.findings if f.status == AnalysisStatus.SUPPORTED)
    assert finding_violations(concluded) == []
    assert finding_violations(concluded.model_copy(update={"evidence_ids": []}))
    assert finding_violations(concluded.model_copy(update={"path": []}))
    with_terminal = next(f for f in result.findings if f.limitations and f.status == "supported")
    assert finding_violations(with_terminal.model_copy(update={"limitations": []}))

    failed = analysis(AnalyzerKind.OWNERSHIP, "2", "failed")
    stripped = failed.model_copy(
        update={"findings": [f.model_copy(update={"limitations": []}) for f in failed.findings]}
    )
    assert result_violations(stripped, limited=True)
    upgraded = failed.model_copy(
        update={
            "findings": [
                f.model_copy(update={"status": AnalysisStatus.SUPPORTED}) for f in failed.findings
            ]
        }
    )
    assert result_violations(upgraded, limited=True)


# ---- Azure: generic mechanics work, AWS semantics are not reused -------------------


AZURE = FIXTURES_ROOT / "snapshots" / "azure.json"


@pytest.mark.parametrize("kind", KINDS)
def test_azure_semantic_analyses_are_explicitly_unsupported(kind):
    from archdiscov.services import load_snapshot

    index = GraphIndex(load_snapshot(AZURE))
    parameters = {**SCENARIOS[kind].parameters, "kind": kind}
    if "resource_id" in parameters:
        parameters["resource_id"] = index.resource_ids[0]
    result = run_analyzer(index, parameters)
    assert result_violations(result) == []
    if kind == AnalyzerKind.OWNERSHIP:  # reads recorded tags without branching on cloud
        assert result.findings
        return
    [finding] = result.findings
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert [limitation.code for limitation in finding.limitations] == ["unsupported_platform"]


def test_generic_mechanics_still_work_on_azure():
    from archdiscov.services import compare_snapshots, load_snapshot

    azure = load_snapshot(AZURE)
    assert search_resources(GraphIndex(azure), {}).resources
    result = compare_snapshots(azure, azure)
    assert result.matched and not result.unmatched and result.limitations == []


# ---- results are immutable and verified -------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_saved_results_are_immutable_and_verified(tmp_path, kind):
    source, result = snapshot(kind, CURRENT_SCHEMA), analysis(kind, CURRENT_SCHEMA)
    path = save_analysis(result, tmp_path / "result.json", snapshot=source)
    original = path.read_bytes()
    with pytest.raises(SnapshotConflictError):
        save_analysis(result, path, snapshot=source)
    assert path.read_bytes() == original
    assert load_analysis(path, snapshot=source) == result

    summary = result.findings[0].summary
    tampered = original.replace(summary.encode(), (summary + " (edited)").encode(), 1)
    assert tampered != original
    edited = tmp_path / "edited.json"
    edited.write_bytes(tampered)
    for check in (None, source):
        with pytest.raises(AnalysisValidationError) as error:
            load_analysis(edited, snapshot=check)
        assert error.value.code == "invalid_analysis_result"
        assert summary not in repr(error.value.details)

    other = snapshot(next(item for item in KINDS if item != kind), CURRENT_SCHEMA)
    assert other.snapshot_id != source.snapshot_id
    with pytest.raises(AnalysisValidationError):
        load_analysis(path, snapshot=other)
    assert path.read_bytes() == original
