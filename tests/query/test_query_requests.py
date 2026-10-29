import pytest
from pydantic import ValidationError

from archdiscov.models.enums import (
    DependencyRelationship,
    QueryDirection,
    QueryEdgeSelection,
)
from archdiscov.models.query.requests import (
    QueryInspectRequest,
    QueryNeighboursRequest,
    QueryPathsRequest,
    QueryPhysicalIdentifierFilter,
    QuerySearchRequest,
    QueryTagFilter,
)


def test_neighbour_request_defaults():
    request = QueryNeighboursRequest(resource_id="resource-1")

    assert request.resource_id == "resource-1"
    assert request.options.direction == QueryDirection.OUTGOING
    assert request.options.edge_selection == QueryEdgeSelection.DEPENDENCIES
    assert request.options.relationships == []
    assert request.options.attachment_kinds == []
    assert request.limit_result == 100


def test_neighbour_request_preserves_options_through_serialization():
    request = QueryNeighboursRequest.model_validate(
        {
            "resource_id": "resource-1",
            "options": {
                "direction": "incoming",
                "relationships": ["attached_to"],
            },
            "limit_result": 25,
        }
    )

    assert request.options.direction == QueryDirection.INCOMING
    assert request.options.relationships == [DependencyRelationship.ATTACHED_TO]
    assert request.limit_result == 25
    assert QueryNeighboursRequest.model_validate_json(request.model_dump_json()) == request


# Every request string field shares NonEmptyString, so one field proves both constraints.
@pytest.mark.parametrize(
    "resource_id,error_type",
    [("", "string_too_short"), (" ", "string_pattern_mismatch")],
    ids=["empty", "whitespace"],
)
def test_neighbour_request_rejects_blank_resource_id(resource_id, error_type):
    with pytest.raises(ValidationError) as caught:
        QueryNeighboursRequest(resource_id=resource_id)

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("resource_id",)
    assert errors[0]["type"] == error_type


def test_neighbour_request_applies_nested_filter_validation():
    with pytest.raises(
        ValidationError,
        match="Dependency filters cannot be used with containment-only selection",
    ) as caught:
        QueryNeighboursRequest.model_validate(
            {
                "resource_id": "resource-1",
                "options": {
                    "edge_selection": "containment",
                    "relationships": ["attached_to"],
                },
            }
        )
    assert caught.value.errors()[0]["loc"] == ("options",)


def test_path_request_defaults():
    request = QueryPathsRequest(resource_id="resource-1")

    assert request.resource_id == "resource-1"
    assert request.options.direction == QueryDirection.OUTGOING
    assert request.options.edge_selection == QueryEdgeSelection.DEPENDENCIES
    assert request.limits.depth == 3
    assert request.limits.limit_result == 100
    assert request.limits.work_limit == 10_000


def test_path_request_preserves_options_and_limits():
    request = QueryPathsRequest.model_validate(
        {
            "resource_id": "resource-1",
            "options": {"direction": "both", "edge_selection": "both"},
            "limits": {"depth": 2, "limit_result": 10, "work_limit": 50},
        }
    )

    assert request.options.direction == QueryDirection.BOTH
    assert request.options.edge_selection == QueryEdgeSelection.BOTH
    assert request.limits.depth == 2
    assert request.limits.limit_result == 10
    assert request.limits.work_limit == 50
    assert QueryPathsRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize(
    "field,value,error_type",
    [
        ("depth", 0, "greater_than_equal"),
        ("limit_result", 10_001, "less_than_equal"),
        ("work_limit", 0, "greater_than_equal"),
    ],
)
def test_path_request_applies_nested_limit_validation(field, value, error_type):
    with pytest.raises(ValidationError) as caught:
        QueryPathsRequest.model_validate(
            {
                "resource_id": "resource-1",
                "limits": {field: value},
            }
        )
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("limits", field)
    assert errors[0]["type"] == error_type


def test_inspect_request_round_trip():
    request = QueryInspectRequest(resource_id="resource-1")

    assert request.resource_id == "resource-1"
    assert QueryInspectRequest.model_validate_json(request.model_dump_json()) == request


def test_inspect_request_rejects_traversal_options():
    with pytest.raises(ValidationError) as caught:
        QueryInspectRequest.model_validate({"resource_id": "resource-1", "options": {}})

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("options",)
    assert errors[0]["type"] == "extra_forbidden"


def test_search_request_defaults():
    request = QuerySearchRequest()

    assert request.native_type is None
    assert request.name is None
    assert request.limit_result == 100
    assert request.resource_id is None
    assert request.physical_identifier is None


def test_search_request_preserves_filters():
    request = QuerySearchRequest(
        native_type="Microsoft.Network/virtualNetworks",
        name="Example Network",
        limit_result=25,
    )

    assert request.native_type == "Microsoft.Network/virtualNetworks"
    assert request.name == "Example Network"
    assert request.limit_result == 25
    assert QuerySearchRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("value", [None, "payments", ""])
def test_search_request_preserves_tag_filter(value):
    request = QuerySearchRequest(
        tag=QueryTagFilter(key="Team", value=value),
    )

    assert request.tag.key == "Team"
    assert request.tag.value == value
    assert QuerySearchRequest.model_validate_json(request.model_dump_json()) == request


def test_tag_filter_rejects_value_without_key():
    with pytest.raises(ValidationError) as caught:
        QuerySearchRequest.model_validate({"tag": {"value": "payments"}})

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("tag", "key")
    assert errors[0]["type"] == "missing"


def test_search_request_preserves_resource_id():
    request = QuerySearchRequest(resource_id="resource-1")

    assert request.resource_id == "resource-1"
    assert QuerySearchRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize(
    "scope,native_type,physical_id",
    [
        (
            {"platform": "aws", "account_id": "123456789012"},
            "AWS::EC2::Instance",
            "i-example",
        ),
        (
            {
                "platform": "azure",
                "subscription_id": "11111111-1111-1111-1111-111111111111",
            },
            "Microsoft.Compute/virtualMachines",
            "/subscriptions/11111111-1111-1111-1111-111111111111"
            "/resourceGroups/example/providers/Microsoft.Compute"
            "/virtualMachines/example",
        ),
    ],
)
def test_search_request_preserves_physical_identifier(scope, native_type, physical_id):
    request = QuerySearchRequest(
        physical_identifier=QueryPhysicalIdentifierFilter(
            scope=scope,
            native_type=native_type,
            physical_id=physical_id,
        )
    )

    identifier = request.physical_identifier
    assert identifier.scope.model_dump(mode="json")["platform"] == scope["platform"]
    assert identifier.native_type == native_type
    assert identifier.physical_id == physical_id
    assert QuerySearchRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("field", ["scope", "native_type", "physical_id"])
def test_physical_identifier_requires_qualification(field):
    data = {
        "scope": {"platform": "aws", "account_id": "123456789012"},
        "native_type": "AWS::EC2::Instance",
        "physical_id": "i-example",
    }
    del data[field]

    with pytest.raises(ValidationError) as caught:
        QueryPhysicalIdentifierFilter.model_validate(data)

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == (field,)
    assert errors[0]["type"] == "missing"


def test_search_request_applies_physical_identifier_scope_validation():
    with pytest.raises(ValidationError) as caught:
        QuerySearchRequest.model_validate(
            {
                "physical_identifier": {
                    "scope": {"platform": "aws", "account_id": "invalid"},
                    "native_type": "AWS::EC2::Instance",
                    "physical_id": "i-example",
                }
            }
        )

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("physical_identifier", "scope", "aws", "account_id")
    assert errors[0]["type"] == "string_pattern_mismatch"
