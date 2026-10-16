import pytest
from pydantic import ValidationError

from archdiscov.models.enums import (
    AttachmentKind,
    DependencyRelationship,
    QueryDirection,
    QueryEdgeSelection,
)
from archdiscov.models.query_parameters import QueryPathLimits, QueryTraversalOptions


def test_traversal_defaults():
    options = QueryTraversalOptions()

    assert options.direction == QueryDirection.OUTGOING
    assert options.edge_selection == QueryEdgeSelection.DEPENDENCIES
    assert options.relationships == []
    assert options.attachment_kinds == []


@pytest.mark.parametrize(
    "selection",
    [QueryEdgeSelection.DEPENDENCIES, QueryEdgeSelection.BOTH],
)
def test_dependency_filters_are_preserved(selection):
    options = QueryTraversalOptions(
        direction=QueryDirection.INCOMING,
        edge_selection=selection,
        relationships=[DependencyRelationship.ATTACHED_TO],
        attachment_kinds=[AttachmentKind.SECURITY_GROUP_MEMBERSHIP],
    )

    assert options.direction == QueryDirection.INCOMING
    assert options.edge_selection == selection
    assert options.relationships == [DependencyRelationship.ATTACHED_TO]
    assert options.attachment_kinds == [AttachmentKind.SECURITY_GROUP_MEMBERSHIP]
    assert QueryTraversalOptions.model_validate_json(options.model_dump_json()) == options


def test_containment_without_dependency_filters_is_valid():
    options = QueryTraversalOptions(
        edge_selection=QueryEdgeSelection.CONTAINMENT,
    )

    assert options.edge_selection == QueryEdgeSelection.CONTAINMENT


@pytest.mark.parametrize(
    "filters",
    [
        {"relationships": [DependencyRelationship.ATTACHED_TO]},
        {"attachment_kinds": [AttachmentKind.SECURITY_GROUP_MEMBERSHIP]},
    ],
)
def test_containment_rejects_dependency_filters(filters):
    with pytest.raises(
        ValidationError,
        match="Dependency filters cannot be used with containment-only selection",
    ):
        QueryTraversalOptions(
            edge_selection=QueryEdgeSelection.CONTAINMENT,
            **filters,
        )


@pytest.mark.parametrize(
    "field,value,error_type",
    [
        ("direction", "invalid", "enum"),
        ("edge_selection", "invalid", "enum"),
        ("relationships", ["invalid"], "enum"),
        ("attachment_kinds", ["invalid"], "enum"),
        ("unexpected", True, "extra_forbidden"),
    ],
)
def test_invalid_options_are_rejected(field, value, error_type):
    with pytest.raises(ValidationError) as caught:
        QueryTraversalOptions.model_validate({field: value})
    error = caught.value.errors()[0]
    assert error["loc"][0] == field
    assert error["type"] == error_type


def test_path_limit_defaults():
    limits = QueryPathLimits()

    assert limits.depth == 3
    assert limits.limit_result == 100
    assert limits.work_limit == 10_000


@pytest.mark.parametrize(
    "field,value",
    [
        ("depth", 1),
        ("depth", 10),
        ("limit_result", 1),
        ("limit_result", 10_000),
        ("work_limit", 1),
        ("work_limit", 1_000_000),
    ],
)
def test_path_limit_boundaries_are_accepted(field, value):
    limits = QueryPathLimits.model_validate({field: value})

    assert getattr(limits, field) == value
    assert QueryPathLimits.model_validate_json(limits.model_dump_json()) == limits


@pytest.mark.parametrize(
    "field,value,error_type",
    [
        ("depth", 0, "greater_than_equal"),
        ("depth", 11, "less_than_equal"),
        ("limit_result", 0, "greater_than_equal"),
        ("limit_result", 10_001, "less_than_equal"),
        ("work_limit", 0, "greater_than_equal"),
        ("work_limit", 1_000_001, "less_than_equal"),
    ],
)
def test_out_of_range_path_limits_are_rejected(field, value, error_type):
    with pytest.raises(ValidationError) as caught:
        QueryPathLimits.model_validate({field: value})

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == (field,)
    assert errors[0]["type"] == error_type
