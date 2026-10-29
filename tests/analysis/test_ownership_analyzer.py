import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalysisStatus
from archdiscov.services import run_analyzer, validate_analysis_result
from tests.support.tagged_snapshots import TAG_EVIDENCE, tagged

# The analyzer reads both clouds through the graph, which is tested across schemas itself.
SOURCES = [("snapshots/hierarchy.json", True), ("snapshots/azure.json", False)]
IDS = ["aws", "azure"]


def analyze(snapshot, keys=None):
    parameters = {"kind": "ownership"}
    if keys is not None:
        parameters["ownership_keys"] = keys
    result = run_analyzer(GraphIndex(snapshot), parameters)
    assert validate_analysis_result(result, snapshot) == result
    return {item.resources[0].resource_id: item for item in result.findings}


def codes(finding):
    return [limitation.code for limitation in finding.limitations]


@pytest.mark.parametrize(("source", "upgraded"), SOURCES, ids=IDS)
class TestOwnershipSignals:
    def test_only_a_nonblank_exact_owner_key_counts(self, source, upgraded):
        for tags in ({"Owner": "platform"}, {"Team": "web"}):
            snapshot, target = tagged(source, upgraded, tags=tags)
            assert target not in analyze(snapshot)
        for tags in ({"owner": "x"}, {"Owner": "  "}):
            snapshot, target = tagged(source, upgraded, tags=tags)
            assert target in analyze(snapshot)

    def test_missing_tags_with_cited_map_are_a_supported_signal(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Name": "app"})
        finding = analyze(snapshot)[target]
        assert finding.status in (AnalysisStatus.SUPPORTED, AnalysisStatus.PARTIAL)
        assert finding.evidence_ids == [TAG_EVIDENCE]
        assert "not proof that nobody owns" in finding.summary
        assert "Owner or Team" in finding.summary
        assert "tags_unavailable" not in codes(finding)

    def test_custom_keys_replace_the_defaults(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Owner": "a", "CostCentre": "7"})
        assert target in analyze(snapshot, ["Squad"])
        assert target not in analyze(snapshot, ["Squad", "CostCentre"])
        assert target not in analyze(snapshot, ["Owner"])

    def test_uncited_tags_are_insufficient_evidence_not_absence(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Name": "app"}, cite=None)
        finding = analyze(snapshot)[target]
        assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
        assert finding.evidence_ids == []
        assert "tags_unavailable" in codes(finding)
        assert "proof" in finding.limitations[0].message
        assert finding.summary == "Ownership tags cannot be assessed from the recorded data."


def test_incomplete_coverage_downgrades_a_signal_but_never_upgrades_unavailable_tags():
    snapshot, target = tagged("snapshots/partial-coverage.json", tags={"Name": "app"})
    finding = analyze(snapshot)[target]
    assert finding.status == AnalysisStatus.PARTIAL
    assert codes(finding) == ["coverage_failed"]
    assert finding.evidence_ids == [TAG_EVIDENCE]
    snapshot, target = tagged("snapshots/partial-coverage.json", cite=None)
    finding = analyze(snapshot)[target]
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert codes(finding) == ["tags_unavailable", "coverage_failed"]
