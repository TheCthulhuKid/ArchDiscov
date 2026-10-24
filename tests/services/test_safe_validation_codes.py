import json
from collections.abc import Callable
from copy import deepcopy

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.snapshot_access import upgrade_snapshot
from archdiscov.models.validation_errors import SAFE_VALIDATION_MESSAGES, validation_error
from archdiscov.services.snapshots import (
    SnapshotValidationError,
    _safe_validation_message,
    validate_snapshot,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
SECRET = "duplicate resource unknown resource owner SECRET_PRIVATE_TOKEN"


@pytest.mark.parametrize(
    "message",
    [
        "entirely revised validator wording",
        "duplicate resource SECRET at private/location",
        "unknown resource owner SECRET; missing key SECRET",
    ],
)
def test_custom_code_renderer_ignores_message_input_and_context(message: str) -> None:
    error = {
        "type": "snapshot_duplicate_resource",
        "msg": message,
        "input": {"password": "SECRET"},
        "ctx": {"detail": "SECRET", "error": ValueError("SECRET")},
    }
    assert _safe_validation_message(error) == "Snapshot constraint failed: duplicate resource."


def test_unknown_code_uses_generic_safe_fallback() -> None:
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


@pytest.mark.parametrize("code", SAFE_VALIDATION_MESSAGES)
def test_all_registered_codes_ignore_even_conflicting_private_details(code: str) -> None:
    private = validation_error(code, SECRET)
    assert isinstance(private, PydanticCustomError)
    assert private.type == code
    assert str(private) == SECRET
    error = ValidationError.from_exception_data(
        "PrivateValidation", [{"type": private, "loc": (), "input": {"password": SECRET}}]
    ).errors(include_url=False)[0]
    assert error["ctx"] == {"detail": SECRET}
    assert error["input"] == {"password": SECRET}
    assert _safe_validation_message(error) == SAFE_VALIDATION_MESSAGES[code]
    assert _safe_validation_message({"type": code}) == SAFE_VALIDATION_MESSAGES[code]


def test_unregistered_validator_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unregistered validation code"):
        validation_error("unregistered", SECRET)


@pytest.mark.parametrize("version", ["1", "3"])
@pytest.mark.parametrize(
    "defect,code,safe_message",
    [
        (
            "citation",
            "snapshot_evidence_citation_unknown",
            "Snapshot constraint failed: unknown evidence citation.",
        ),
        (
            "missing_key",
            "snapshot_fact_pointer_missing_key",
            "Snapshot constraint failed: missing key.",
        ),
        (
            "invalid_index",
            "snapshot_fact_pointer_invalid_index",
            "Snapshot constraint failed: invalid array index.",
        ),
        (
            "out_of_bounds",
            "snapshot_fact_pointer_index_out_of_bounds",
            "Snapshot constraint failed: array index out of bounds.",
        ),
        (
            "scalar",
            "snapshot_fact_pointer_scalar",
            "Snapshot constraint failed: cannot traverse scalar.",
        ),
    ],
)
def test_shared_checks_keep_private_details_but_publish_only_codes(
    version: str, defect: str, code: str, safe_message: str
) -> None:
    model = CloudSnapshot if version == "3" else ArchitectureSnapshot
    data = json.loads((FIXTURES / ("azure.json" if version == "3" else "aws.json")).read_text())
    validated = model.model_validate(data)
    resource_id = next(iter(validated.regions.values())).resources[0].id
    resource = next(iter(data["regions"].values()))["resources"][0]
    if defect == "citation":
        resource["evidence_ids"] = [SECRET]
    else:
        evidence_id = "network-address"
        if version == "1":
            evidence_id = "pointer-evidence"
            data["evidence"] = {
                evidence_id: {
                    "snapshot_id": data["snapshot_id"],
                    "resource_id": resource_id,
                    "source": "mock",
                    "locator": "safe-validation-test",
                    "configuration_path": "Resource",
                    "value": None,
                }
            }
            model.model_validate(data)
        resource["attributes"] = {"array": [None], "scalar": None}
        pointer = {
            "missing_key": f"/attributes/{SECRET}",
            "invalid_index": f"/attributes/array/{SECRET}",
            "out_of_bounds": "/attributes/array/999999999999999999999999999999999999",
            "scalar": f"/attributes/scalar/{SECRET}",
        }[defect]
        resource["fact_evidence"] = {pointer: [evidence_id]}
    with pytest.raises(ValidationError) as direct:
        model.model_validate(data)
    private = direct.value.errors()[0]
    assert private["type"] == code
    assert private["msg"].startswith(f"resource {resource_id!r}:")
    assert private["ctx"]["detail"] == private["msg"]
    if defect != "out_of_bounds":
        assert SECRET in private["msg"]
        assert SECRET in private["ctx"]["detail"]
    with pytest.raises(SnapshotValidationError) as public:
        validate_snapshot(data)
    assert len(public.value.details) == 1
    issue = public.value.details[0]
    assert issue.type == code
    assert issue.message == safe_message
    assert SECRET not in str(public.value)
    assert SECRET not in repr(public.value.details)
    assert not hasattr(issue, "input")
    assert not hasattr(issue, "ctx")
    assert isinstance(public.value.__cause__, ValidationError)


ROOT = ("regions", "eastus", "resources", 0)
ADDRESS = ("evidence", "network-address")
OTHER_ID = "22222222-2222-2222-2222-222222222222"


def _set(*path: str | int, value: object) -> Callable[[dict], None]:
    """A defect that sets the value at `path` in the snapshot data."""

    def apply(data: dict) -> None:
        *parents, last = path
        for key in parents:
            data = data[key]
        data[last] = value

    return apply


def _other_subscription(data: dict) -> None:
    data["coverage"]["scope"]["subscription_id"] = OTHER_ID
    for operation in data["coverage"]["operations"]:
        operation["scope"] = deepcopy(data["coverage"]["scope"])


def _scope_resource(data: dict) -> None:
    data["scope_resources"] = [deepcopy(data["regions"]["eastus"]["resources"][0])]


def _other_native_type(data: dict) -> None:
    identity = data["regions"]["eastus"]["resources"][0]["identity"]
    identity["native_type"] = "Microsoft.Network/other"
    identity["physical_id"] = identity["physical_id"].replace(
        "/virtualNetworks/network", "/other/network"
    )


def _duplicate(data: dict) -> None:
    resources = data["regions"]["eastus"]["resources"]
    resources.append(deepcopy(resources[0]))


# One defect of the Azure fixture per validation code it must raise.
DEFECTS: dict[str, Callable[[dict], None]] = {
    "cloud_snapshot_source_platform_mismatch": _set(
        "source", value={"platform": "aws", "provenance": {"kind": "aws"}}
    ),
    "cloud_snapshot_coverage_scope_mismatch": _other_subscription,
    "cloud_snapshot_locations_uncovered": _set("regions", "westus", value={"resources": []}),
    "cloud_snapshot_scope_coverage_required": _scope_resource,
    "cloud_snapshot_resource_location_mismatch": _set(
        *ROOT, "identity", "location", value="westus"
    ),
    "cloud_snapshot_evidence_context_mismatch": _set(
        *ADDRESS, "service", value="Microsoft.Compute"
    ),
    "cloud_snapshot_aws_evidence_scope_required": _set(*ADDRESS, "source", value="aws"),
    "cloud_snapshot_lifecycle_source_invalid": _set(
        *ROOT, "lifecycle", value={"actions": ["create"]}
    ),
    "cloud_snapshot_dependency_source_unknown": _set("dependencies", 0, "source_id", value=SECRET),
    "cloud_snapshot_dependency_target_unknown": _set(
        "dependencies", 0, "target", "resource_id", value=SECRET
    ),
    "cloud_snapshot_dependency_location_mismatch": _set(
        "dependencies", 0, "location", value="westus"
    ),
    "azure_resource_native_type_mismatch": _other_native_type,
    "snapshot_duplicate_resource": _duplicate,
    "snapshot_evidence_snapshot_mismatch": _set(*ADDRESS, "snapshot_id", value=OTHER_ID),
    "snapshot_evidence_owner_unknown": _set(*ADDRESS, "resource_id", value=SECRET),
    "snapshot_evidence_citation_owner_mismatch": _set(*ROOT, "evidence_ids", value=["vm-size"]),
    "snapshot_unresolved_resource_unknown": _set(
        "coverage", "operations", 1, "unresolved_values", 0, "resource_id", value=SECRET
    ),
    "snapshot_collection_window_invalid": _set(
        "collection_completed_at", value="2020-01-01T00:00:00Z"
    ),
    "snapshot_operation_window_invalid": _set(
        "collection_started_at", value="2026-10-13T08:00:01Z"
    ),
}


@pytest.mark.parametrize("code", list(DEFECTS))
def test_cloud_and_azure_validators_emit_registered_codes(code: str) -> None:
    data = json.loads((FIXTURES / "azure.json").read_text())
    DEFECTS[code](data)
    with pytest.raises(ValidationError) as direct:
        CloudSnapshot.model_validate(data)
    assert direct.value.errors()[0]["type"] == code
    with pytest.raises(SnapshotValidationError) as public:
        validate_snapshot(data)
    assert len(public.value.details) == 1
    assert public.value.details[0].type == code
    assert public.value.details[0].message == SAFE_VALIDATION_MESSAGES[code]
    assert SECRET not in repr(public.value.details)


def test_azure_evidence_requires_azure_scope_code() -> None:
    legacy = ArchitectureSnapshot.model_validate_json((FIXTURES / "hierarchy.json").read_text())
    data = upgrade_snapshot(legacy).model_dump(mode="json")
    next(iter(data["evidence"].values()))["source"] = "azure"
    with pytest.raises(ValidationError) as direct:
        CloudSnapshot.model_validate(data)
    code = "cloud_snapshot_azure_evidence_scope_required"
    assert direct.value.errors()[0]["type"] == code
    assert direct.value.errors()[0]["msg"] == "Azure evidence requires Azure scope"
    with pytest.raises(SnapshotValidationError) as public:
        validate_snapshot(data)
    assert len(public.value.details) == 1
    assert public.value.details[0].type == code
    assert public.value.details[0].message == SAFE_VALIDATION_MESSAGES[code]


def test_unmigrated_value_errors_retain_incremental_compatibility() -> None:
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
