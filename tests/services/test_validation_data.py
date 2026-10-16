from collections import UserDict
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from archdiscov.services.snapshots import _detach_data
from archdiscov.services.validation_data import detach_data


class NestedData(BaseModel):
    values: list[dict[str, list[int]]]
    identifier: UUID
    timestamp: datetime


def test_nested_models_and_containers_are_detached():
    model = NestedData(
        values=[{"items": [1, 2]}],
        identifier=UUID("00000000-0000-4000-8000-000000000001"),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    source = UserDict({"nested": (model, {"items": [3]})})
    detached = detach_data(source)
    assert detached == {
        "nested": [
            {
                "values": [{"items": [1, 2]}],
                "identifier": str(model.identifier),
                "timestamp": "2026-01-01T00:00:00Z",
            },
            {"items": [3]},
        ]
    }
    detached["nested"][0]["values"][0]["items"].append(4)
    detached["nested"][1]["items"].append(5)
    assert model.values == [{"items": [1, 2]}]
    assert source["nested"][1] == {"items": [3]}


def test_snapshot_private_import_is_same_helper():
    assert _detach_data is detach_data


def test_model_serialization_does_not_ignore_mutated_invalid_data():
    model = NestedData(
        values=[],
        identifier=UUID("00000000-0000-4000-8000-000000000001"),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    model.values.append("invalid")
    with pytest.raises(PydanticSerializationError):
        detach_data(model)
