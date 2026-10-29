import json

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.validation_errors import SAFE_VALIDATION_MESSAGES, validation_error
from archdiscov.services.snapshots import (
    SnapshotValidationError,
    _safe_validation_message,
    validate_snapshot,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
SECRET = "duplicate resource unknown resource owner SECRET_PRIVATE_TOKEN"


def test_all_registered_codes_ignore_even_conflicting_private_details() -> None:
    for code, safe in SAFE_VALIDATION_MESSAGES.items():
        private = validation_error(code, SECRET)
        assert isinstance(private, PydanticCustomError)
        assert private.type == code
        assert str(private) == SECRET
        error = ValidationError.from_exception_data(
            "PrivateValidation", [{"type": private, "loc": (), "input": {"password": SECRET}}]
        ).errors(include_url=False)[0]
        assert error["ctx"] == {"detail": SECRET}
        assert _safe_validation_message(error) == safe
        assert _safe_validation_message({"type": code}) == safe


def test_a_failed_check_keeps_private_details_but_publishes_only_its_code() -> None:
    data = json.loads((FIXTURES / "azure.json").read_text())
    resource_id = next(iter(CloudSnapshot.model_validate(data).regions.values())).resources[0].id
    resource = next(iter(data["regions"].values()))["resources"][0]
    resource["fact_evidence"] = {f"/attributes/{SECRET}": ["network-address"]}
    code = "snapshot_fact_pointer_missing_key"
    with pytest.raises(ValidationError) as direct:
        CloudSnapshot.model_validate(data)
    private = direct.value.errors()[0]
    assert private["type"] == code
    assert private["msg"].startswith(f"resource {resource_id!r}:")
    assert SECRET in private["msg"] and private["ctx"]["detail"] == private["msg"]
    with pytest.raises(SnapshotValidationError) as public:
        validate_snapshot(data)
    [issue] = public.value.details
    assert (issue.type, issue.message) == (code, "Snapshot constraint failed: missing key.")
    assert SECRET not in str(public.value)
    assert SECRET not in repr(public.value.details)
    assert not hasattr(issue, "input")
    assert not hasattr(issue, "ctx")
    assert isinstance(public.value.__cause__, ValidationError)


def test_unregistered_errors_fall_back_to_safe_messages() -> None:
    assert (
        _safe_validation_message(
            {
                "type": "unknown_SECRET_code",
                "msg": "duplicate resource SECRET",
                "input": "SECRET",
                "ctx": {"detail": "SECRET"},
            }
        )
        == "Invalid snapshot field."
    )
    assert (
        _safe_validation_message(
            {"type": "value_error", "msg": "Value error, coverage locations must be unique SECRET"}
        )
        == "Snapshot constraint failed: coverage locations must be unique."
    )
    assert (
        _safe_validation_message(
            {"type": "value_error", "msg": "unrecognized private wording SECRET"}
        )
        == "Invalid snapshot field."
    )
