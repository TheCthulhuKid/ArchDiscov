import json

import pytest
from pydantic import ValidationError

from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.resources.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork
from archdiscov.models.resources.cloud_unknown import CloudUnknownResource
from archdiscov.services import SnapshotValidationError, validate_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "azure.json"
OTHER_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def azure_data() -> dict:
    return json.loads(FIXTURE.read_text())


def resources(data: dict) -> tuple[dict, dict, dict]:
    network = data["regions"]["eastus"]["resources"][0]
    subnet = network["children"][0]
    return network, subnet, subnet["children"][0]


def test_offline_fixture_has_typed_hierarchy_and_explicit_facts(azure_data: dict) -> None:
    snapshot = CloudSnapshot.model_validate(azure_data)
    network = snapshot.regions["eastus"].resources[0]
    subnet = network.children[0]
    vm = subnet.children[0]
    assert isinstance(network, AzureVirtualNetwork)
    assert isinstance(subnet, AzureSubnet)
    assert isinstance(vm, AzureVirtualMachine)
    assert [str(value) for value in network.address_spaces] == ["10.0.0.0/16"]
    assert [str(value) for value in subnet.address_prefixes] == ["10.0.0.0/24"]
    assert vm.attributes == {"password": "[REDACTED]"}
    assert vm.fact_evidence == {"/vm_size": ["vm-size"]}
    assert snapshot.evidence["vm-unavailable"].value_status == "unresolved"
    for resource in (network, subnet, vm):
        assert resource.identity.scope == snapshot.scope
        assert resource.id.startswith(f"azure/{snapshot.scope.subscription_id}/physical/")
    assert snapshot.dependencies[0].target.resource_id == subnet.id
    assert CloudSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def _vm_identity(data: dict) -> dict:
    return resources(data)[2]["identity"]


def _vm_in_other_subscription(data: dict) -> None:
    identity = _vm_identity(data)
    identity["subscription_id"] = OTHER_SUBSCRIPTION
    identity["physical_id"] = identity["physical_id"].replace(
        data["scope"]["subscription_id"], OTHER_SUBSCRIPTION
    )


def _vm_with_disk_arm(data: dict) -> None:
    identity = _vm_identity(data)
    identity["physical_id"] = identity["physical_id"].replace("/virtualMachines/", "/disks/")


# Azure-specific integrity defects; the shared checks (duplicates, citations, endpoints) are
# covered for schema 3 in `test_cloud_snapshot.py`.
AZURE_DEFECTS = {
    "resource_scope": (_vm_in_other_subscription, "subscription/tenant must match"),
    "resource_location": (
        lambda data: _vm_identity(data).update(location="westus"),
        "location must match enclosing location",
    ),
    "arm_type": (_vm_with_disk_arm, "type must match"),
    "evidence_scope": (
        lambda data: data["evidence"]["vm-size"]["scope"].update(
            subscription_id=OTHER_SUBSCRIPTION
        ),
        "evidence context must match",
    ),
    "operation_scope": (
        lambda data: data["coverage"]["operations"][0]["scope"].update(
            subscription_id=OTHER_SUBSCRIPTION
        ),
        "scope must match",
    ),
}


@pytest.mark.parametrize("defect", AZURE_DEFECTS)
def test_isolated_azure_integrity_defects(azure_data: dict, defect: str) -> None:
    corrupt, diagnostic = AZURE_DEFECTS[defect]
    corrupt(azure_data)
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(azure_data)
    assert error.value.code == "invalid_snapshot"
    assert any(diagnostic in issue.message for issue in error.value.details), error.value.details


def unknown_resource(azure_data: dict) -> dict:
    return {
        "resource_type": "cloud_unknown",
        "platform": "azure",
        "native_type": "Microsoft.Storage/storageAccounts",
        "identity": {
            **azure_data["scope"],
            "location": "eastus",
            "resource_group": "example",
            "service": "Microsoft.Storage",
            "native_type": "Microsoft.Storage/storageAccounts",
            "logical_address": "fixture.storage",
        },
        "attributes": {"fixture_field": "retained"},
    }


def test_schema3_unknown_resource_retains_native_type_and_scope(azure_data: dict) -> None:
    azure_data["regions"]["eastus"]["resources"].append(unknown_resource(azure_data))
    snapshot = CloudSnapshot.model_validate(azure_data)
    unknown = snapshot.regions["eastus"].resources[1]
    assert isinstance(unknown, CloudUnknownResource)
    assert unknown.native_type == "Microsoft.Storage/storageAccounts"
    assert unknown.identity.scope == snapshot.scope
    assert unknown.attributes == {"fixture_field": "retained"}


@pytest.mark.parametrize(
    "field,value,diagnostic",
    [
        ("native_type", "Microsoft.Storage/other", "native_type must match"),
        ("platform", "aws", "platform must match identity"),
    ],
)
def test_schema3_unknown_resource_rejects_inconsistent_context(
    azure_data: dict, field: str, value: str, diagnostic: str
) -> None:
    unknown = unknown_resource(azure_data)
    unknown[field] = value
    azure_data["regions"]["eastus"]["resources"].append(unknown)
    with pytest.raises(ValidationError, match=diagnostic):
        CloudSnapshot.model_validate(azure_data)
