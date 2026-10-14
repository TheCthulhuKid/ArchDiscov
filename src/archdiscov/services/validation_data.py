"""Detached current data shared by model validation boundaries."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def detach_data(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json", warnings="error")
    if isinstance(data, Mapping):
        return {key: detach_data(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [detach_data(value) for value in data]
    return data
