import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.cloud.contracts import CloudEvidence
from archdiscov.models.json_pointer import escape_pointer_token
from archdiscov.services import inspect_resource, load_snapshot, search_resources, upgrade_snapshot
from tests.paths import FIXTURES_ROOT


def tagged_index(tags, citation="map", fixture="hierarchy"):
    source = load_snapshot(FIXTURES_ROOT / "snapshots" / f"{fixture}.json")
    if source.schema_version != "3":
        source = upgrade_snapshot(source)
    resource = source.regions[next(iter(source.regions))].resources[0]
    resource.name = "Example Name"
    resource.attributes["tags"] = tags
    if citation:
        pointer = "/attributes/tags"
        if citation == "key":
            pointer += f"/{escape_pointer_token(next(iter(tags)))}"
        evidence = next(iter(source.evidence.values())).model_copy(deep=True)
        record = CloudEvidence.model_validate(
            {
                **evidence.model_dump(mode="json"),
                "resource_id": resource.id,
                "scope": source.scope,
                "value": tags,
                "configuration_path": "Tags",
            }
        )
        source.evidence["test-tags"] = record
        resource.fact_evidence[pointer] = ["test-tags"]
    return GraphIndex(source), resource.id


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
@pytest.mark.parametrize("value", [None, "payments", ""])
def test_tags_and_name_match_exactly_and_combine_with_other_filters(fixture, value):
    tags = {"Team": "payments" if value is None else value}
    index, resource_id = tagged_index(tags, fixture=fixture)
    query = {
        "resource_id": resource_id,
        "name": "Example Name",
        "tag": {"key": "Team", "value": value},
    }
    result = search_resources(index, query)
    assert [item.id for item in result.resources] == [resource_id]
    assert not any(item.kind == "provenance_unavailable" for item in result.limitations)
    assert search_resources(index, {**query, "name": "example name"}).resources == []
    assert search_resources(index, {**query, "tag": {"key": "team"}}).resources == []
    assert (
        search_resources(index, {**query, "tag": {"key": "Team", "value": "PAYMENTS"}}).resources
        == []
    )


@pytest.mark.parametrize("tags", [None, {}, {"Other": "recorded"}])
def test_unavailable_tag_provenance_survives_no_matches(tags):
    index, resource_id = tagged_index(tags, citation=None)
    result = search_resources(index, {"resource_id": resource_id, "tag": {"key": "Team"}})
    assert result.resources == []
    limitations = [item for item in result.limitations if item.kind == "provenance_unavailable"]
    assert len(limitations) == 1
    assert limitations[0].resource_id == resource_id
    assert limitations[0].fact_path == "/attributes/tags"


def test_recorded_empty_tags_are_distinct_from_unavailable_tags():
    index, resource_id = tagged_index({})
    result = search_resources(index, {"resource_id": resource_id, "tag": {"key": "Team"}})
    assert result.resources == []
    assert not any(item.kind == "provenance_unavailable" for item in result.limitations)


def test_exact_leaf_pointer_supports_escaped_tag_key_without_claiming_map_completeness():
    index, resource_id = tagged_index({"Team/~Name": "payments"}, citation="key")
    result = search_resources(index, {"resource_id": resource_id, "tag": {"key": "Team/~Name"}})
    assert [item.id for item in result.resources] == [resource_id]
    assert not any(item.kind == "provenance_unavailable" for item in result.limitations)
    missing = search_resources(index, {"resource_id": resource_id, "tag": {"key": "Other"}})
    assert any(item.kind == "provenance_unavailable" for item in missing.limitations)


def test_uncited_recorded_tags_can_match_but_carry_provenance_limitation():
    index, resource_id = tagged_index({"Team": "payments"}, citation=None)
    result = search_resources(index, {"resource_id": resource_id, "tag": {"key": "Team"}})
    assert [item.id for item in result.resources] == [resource_id]
    assert any(item.kind == "provenance_unavailable" for item in result.limitations)


def test_redacted_tag_value_is_not_searchable_as_a_literal_value():
    index, resource_id = tagged_index({"password": "protected-value"})
    result = search_resources(
        index, {"resource_id": resource_id, "tag": {"key": "password", "value": "[REDACTED]"}}
    )
    assert result.resources == []
    assert any(item.kind == "provenance_unavailable" for item in result.limitations)
    key_only = search_resources(index, {"resource_id": resource_id, "tag": {"key": "password"}})
    assert [item.id for item in key_only.resources] == [resource_id]
    assert (
        "protected-value"
        not in inspect_resource(index, {"resource_id": resource_id}).model_dump_json()
    )


@pytest.mark.parametrize("status", ["omitted", "unresolved"])
def test_unavailable_tag_citation_does_not_become_known_empty(status):
    source = upgrade_snapshot(load_snapshot(FIXTURES_ROOT / "snapshots" / "hierarchy.json"))
    resource = source.regions[next(iter(source.regions))].resources[0]
    resource.attributes["tags"] = {}
    evidence = next(iter(source.evidence.values())).model_dump(mode="json")
    source.evidence["missing-tags"] = CloudEvidence.model_validate(
        {
            **evidence,
            "resource_id": resource.id,
            "value": None,
            "value_status": status,
            "redacted_paths": [],
            "limitation": "Tag source is unavailable.",
        }
    )
    resource.fact_evidence["/attributes/tags"] = ["missing-tags"]
    index = GraphIndex(source)
    result = search_resources(index, {"resource_id": resource.id, "tag": {"key": "Team"}})
    assert result.resources == []
    assert any(item.kind == "provenance_unavailable" for item in result.limitations)
