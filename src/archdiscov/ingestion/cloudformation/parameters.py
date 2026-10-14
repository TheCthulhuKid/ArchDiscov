"""Bounded local parameter files; values never enter public diagnostics."""

import json
from pathlib import Path

from ..adapter import IngestionError
from ..strict_json import reject_constant, unique_object
from .evaluation_limits import MAX_BYTES, validate_json


def load_parameters(path: Path) -> dict:
    """Read a JSON name-to-value object, not a CloudFormation API parameter list."""
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError
        result = json.loads(
            content.decode("utf-8"),
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
