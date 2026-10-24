"""Bounded local parameter files; values never enter public diagnostics."""

import json
from pathlib import Path

from ...core.strict_json import read_bounded, reject_constant, unique_object
from ..adapter import IngestionError
from .evaluation_limits import MAX_BYTES, validate_json


def load_parameters(path: Path) -> dict:
    """Read a JSON name-to-value object, not a CloudFormation API parameter list."""
    try:
        result = json.loads(
            read_bounded(path, MAX_BYTES).decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        validate_json(result)
        if not isinstance(result, dict) or any(not key.strip() for key in result):
            raise ValueError
        return result
    except (OSError, ValueError, RecursionError):
        raise IngestionError(
            "invalid_request", "Parameters file must be a bounded UTF-8 JSON object."
        ) from None
