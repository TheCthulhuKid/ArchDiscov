import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.query.limitations import QueryLimitation, QueryMissingCoverageLimitation

COVERAGE = {
    "scope": {"platform": "aws", "account_id": "123456789012"},
    "service": "iam",
    "operation": "ListRoles",
}
TERMINAL = {"terminal_id": "terminal-1", "edge_id": "edge-1"}


def test_limitation_union_round_trips_every_kind():
    adapter = TypeAdapter(list[QueryLimitation])
    contexts = [
        {"kind": "coverage_missing", "coverage": COVERAGE},
        {"kind": "coverage_limited", "coverage": COVERAGE, "status": "incomplete"},
        {"kind": "provenance_unavailable", "resource_id": "r-1", "fact_path": "/attributes/tags"},
        {
            **TERMINAL,
            "kind": "external_target",
            "target": {"kind": "external", "scope": {"platform": "aws"}, "identifier": "x"},
        },
        {
            **TERMINAL,
            "kind": "unresolved_target",
            "target": {"kind": "unresolved", "reason": "Not collected.", "source_path": "A"},
        },
    ]
    limitations = adapter.validate_python(
        [{**context, "message": "Recorded query limitation."} for context in contexts]
    )

    assert len({type(limitation) for limitation in limitations}) == len(contexts)
    assert adapter.validate_json(adapter.dump_json(limitations)) == limitations


def test_coverage_reference_validates_its_cloud_context():
    with pytest.raises(
        ValidationError, match="scope-wide placement uses null location, not global"
    ):
        QueryMissingCoverageLimitation.model_validate(
            {"coverage": {**COVERAGE, "location": "global"}, "message": "No coverage record."}
        )
