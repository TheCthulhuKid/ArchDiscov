import pytest
from pydantic import ValidationError

from archdiscov.models.enums import DependencyRelationship, QueryEdgeSelection
from archdiscov.models.query.parameters import QueryTraversalOptions


def test_dependency_filters_are_rejected_only_for_containment_only_selection():
    filters = {"relationships": [DependencyRelationship.ATTACHED_TO]}
    both = QueryTraversalOptions(edge_selection=QueryEdgeSelection.BOTH, **filters)
    assert both.relationships == [DependencyRelationship.ATTACHED_TO]
    assert QueryTraversalOptions(edge_selection=QueryEdgeSelection.CONTAINMENT).relationships == []
    with pytest.raises(
        ValidationError,
        match="Dependency filters cannot be used with containment-only selection",
    ):
        QueryTraversalOptions(edge_selection=QueryEdgeSelection.CONTAINMENT, **filters)
