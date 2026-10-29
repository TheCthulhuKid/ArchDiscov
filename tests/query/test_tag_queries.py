import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.cloud.contracts import CloudEvidence
from archdiscov.models.json_pointer import escape_pointer_token
from archdiscov.services import inspect_resource, load_snapshot, search_resources, upgrade_snapshot
from tests.paths import FIXTURES_ROOT


def tagged_index(tags, citation="map"):
    source = upgrade_snapshot(load_snapshot(FIXTURES_ROOT / "snapshots" / "hierarchy.json"))
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


@pytest.mark.parametrize("value", [None, "payments", ""])
def test_tags_and_name_match_exactly_and_combine_with_other_filters(value):
    tags = {"Team": "payments" if value is None else value}
    index, resource_id = tagged_index(tags)
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


# A per-key citation proves that key, not the whole tag map.
@pytest.mark.parametrize(
    "tags,citation,key,matches,limited",
    [
        ({"Team": "payments"}, None, "Team", True, True),
        ({}, None, "Team", False, True),
        ({}, "map", "Team", False, False),
        ({"Team": "payments"}, "key", "Team", True, False),
        ({"Team": "payments"}, "key", "Other", False, True),
    ],
    ids=["uncited-match", "uncited-empty", "cited-empty", "cited-key", "other-key"],
)
def test_tag_provenance_requires_a_present_citation(tags, citation, key, matches, limited):
    index, resource_id = tagged_index(tags, citation=citation)
    result = search_resources(index, {"resource_id": resource_id, "tag": {"key": key}})
    assert [item.id for item in result.resources] == ([resource_id] if matches else [])
    assert any(item.kind == "provenance_unavailable" for item in result.limitations) == limited


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


def test_unavailable_tag_citation_does_not_become_known_empty():
    source = upgrade_snapshot(load_snapshot(FIXTURES_ROOT / "snapshots" / "hierarchy.json"))
    resource = source.regions[next(iter(source.regions))].resources[0]
    resource.attributes["tags"] = {}
    evidence = next(iter(source.evidence.values())).model_dump(mode="json")
    source.evidence["missing-tags"] = CloudEvidence.model_validate(
        {
            **evidence,
            "resource_id": resource.id,
            "value": None,
            "value_status": "unresolved",
            "redacted_paths": [],
            "limitation": "Tag source is unavailable.",
        }
    )
    resource.fact_evidence["/attributes/tags"] = ["missing-tags"]
    index = GraphIndex(source)
    result = search_resources(index, {"resource_id": resource.id, "tag": {"key": "Team"}})
    assert result.resources == []
    assert any(item.kind == "provenance_unavailable" for item in result.limitations)
