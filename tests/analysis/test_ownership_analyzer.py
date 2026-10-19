import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.parameters import OwnershipParameters
from archdiscov.models.enums import AnalysisStatus, EvidenceValueStatus
from archdiscov.services import (
    DEFAULT_REGISTRY,
    load_snapshot,
    run_analyzer,
    upgrade_snapshot,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT
SOURCES = [
    ("analysis/ownership-snapshot.json", False),
    ("snapshots/hierarchy.json", False),
    ("snapshots/hierarchy.json", True),
    ("snapshots/azure.json", False),
    ("snapshots/network.json", False),
]
IDS = ["aws-v2", "aws-v1", "aws-v3", "azure-v3", "aws-network"]
NO_TAGS = object()


def walk(resources):
    for resource in resources:
        yield resource
        yield from walk(resource.children)


def all_resources(snapshot):
    for region in snapshot.regions.values():
        yield from walk(region.resources)
    yield from walk(getattr(snapshot, "global_resources", []))


def tagged(source, upgraded=False, *, tags=NO_TAGS, cite="present"):
    """Tag the first resource that already owns evidence, citing the tag map if asked.

    Returns the snapshot and the tagged resource's ID. The evidence record is cloned
    from one the resource already owns so its cloud context stays consistent.
    """
    snapshot = load_snapshot(FIXTURES / source)
    if upgraded:
        snapshot = upgrade_snapshot(snapshot)
    target = next(item for item in all_resources(snapshot) if item.evidence_ids)
    template = snapshot.evidence[target.evidence_ids[0]]
    attributes = {key: value for key, value in target.attributes.items() if key != "tags"}
    target.attributes = attributes if tags is NO_TAGS else {**attributes, "tags": tags}
    target.fact_evidence.pop("/attributes/tags", None)  # drop any fixture-provided citation
    if cite is not None:
        update = {"configuration_path": "Tags", "value_status": EvidenceValueStatus(cite)}
        if cite == "present":
            update["value"] = tags
        else:
            update.update(value=None, limitation="Tags were not collected.")
        snapshot.evidence["tags-test"] = template.model_copy(update=update)
        target.evidence_ids.append("tags-test")
        target.fact_evidence["/attributes/tags"] = ["tags-test"]
    return snapshot, target.id


def analyze(snapshot, keys=None):
    index = GraphIndex(snapshot)
    parameters = {"kind": "ownership"}
    if keys is not None:
        parameters["ownership_keys"] = keys
    result = run_analyzer(index, parameters)
    assert validate_analysis_result(result, snapshot) == result
    return {item.resources[0].resource_id: item for item in result.findings}, result


def codes(finding):
    return [limitation.code for limitation in finding.limitations]


@pytest.mark.parametrize(("source", "upgraded"), SOURCES, ids=IDS)
class TestOwnershipSignals:
    def test_present_owner_tag_produces_no_finding(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Owner": "platform"})
        assert target not in analyze(snapshot)[0]
        snapshot, target = tagged(source, upgraded, tags={"Team": "web"})
        assert target not in analyze(snapshot)[0]

    def test_missing_tags_with_cited_map_are_a_supported_signal(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Name": "app"})
        finding = analyze(snapshot)[0][target]
        assert finding.status in (AnalysisStatus.SUPPORTED, AnalysisStatus.PARTIAL)
        assert finding.evidence_ids == ["tags-test"]
        assert "not proof that nobody owns" in finding.summary
        assert "Owner or Team" in finding.summary
        assert "tags_unavailable" not in codes(finding)

    @pytest.mark.parametrize("tags", [{}, {"owner": "x"}, {"Owner": ""}, {"Owner": "  "}])
    def test_empty_blank_and_case_mismatched_values_do_not_count(self, source, upgraded, tags):
        snapshot, target = tagged(source, upgraded, tags=tags)
        assert target in analyze(snapshot)[0]

    def test_custom_keys_replace_the_defaults(self, source, upgraded):
        snapshot, target = tagged(source, upgraded, tags={"Owner": "a", "CostCentre": "7"})
        assert target in analyze(snapshot, ["Squad"])[0]
        assert target not in analyze(snapshot, ["Squad", "CostCentre"])[0]
        assert target not in analyze(snapshot, ["Owner"])[0]

    @pytest.mark.parametrize(
        ("tags", "cite"),
        [(NO_TAGS, None), ({"Name": "app"}, None), ({"Name": "app"}, "unresolved")],
        ids=["not-recorded", "uncited", "unresolved-citation"],
    )
    def test_unavailable_tags_are_insufficient_evidence_not_absence(
        self, source, upgraded, tags, cite
    ):
        snapshot, target = tagged(source, upgraded, tags=tags, cite=cite)
        finding = analyze(snapshot)[0][target]
        assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
        assert finding.evidence_ids == []
        assert "tags_unavailable" in codes(finding)
        assert "proof" in finding.limitations[0].message
        assert finding.summary == "Ownership tags cannot be assessed from the recorded data."

    def test_results_are_deterministic_detached_and_sorted(self, source, upgraded):
        snapshot, _ = tagged(source, upgraded, tags={"Name": "app"})
        _, result = analyze(snapshot)
        _, again = analyze(snapshot)
        assert result.findings == again.findings
        ids = [finding.id for finding in result.findings]
        assert ids == sorted(ids)
        assert result.analyzer_version == "1"


def test_incomplete_coverage_downgrades_missing_tag_signal_to_partial():
    snapshot, target = tagged("snapshots/partial-coverage.json", tags={"Name": "app"})
    finding = analyze(snapshot)[0][target]
    assert finding.status == AnalysisStatus.PARTIAL
    assert codes(finding) == ["coverage_failed"]
    assert finding.evidence_ids == ["tags-test"]


def test_incomplete_coverage_keeps_unavailable_tags_insufficient():
    snapshot, target = tagged("snapshots/partial-coverage.json", cite=None)
    finding = analyze(snapshot)[0][target]
    assert finding.status == AnalysisStatus.INSUFFICIENT_EVIDENCE
    assert codes(finding) == ["tags_unavailable", "coverage_failed"]


def test_without_recorded_coverage_a_cited_missing_tag_stays_supported():
    snapshot, target = tagged("snapshots/network.json", tags={"Name": "app"})
    finding = analyze(snapshot)[0][target]
    assert (finding.status, finding.limitations) == (AnalysisStatus.SUPPORTED, [])


def test_ownership_analyzer_is_registered_with_default_parameters():
    assert DEFAULT_REGISTRY.get(OwnershipParameters().kind).version == "1"
    snapshot, _ = tagged("snapshots/network.json", tags={"Name": "app"})
    _, result = analyze(snapshot)
    assert result.parameters.ownership_keys == ["Owner", "Team"]
