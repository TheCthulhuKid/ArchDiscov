import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.models.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork
from archdiscov.models.cloud_snapshot import CloudSnapshot
from archdiscov.models.cloud_unknown import CloudUnknownResource
from archdiscov.services import SnapshotValidationError, validate_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "snapshots" / "azure.json"
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
    assert vm.vm_size is None
    assert vm.attributes == {"password": "[REDACTED]"}
    assert snapshot.source.platform == "azure"
    assert snapshot.source.provenance.kind == "azure_fixture"
    assert snapshot.source.provenance.label == "Offline Azure fixture"
    for resource in (network, subnet, vm):
        assert resource.identity.scope == snapshot.scope
        assert resource.identity.location == "eastus"
        assert resource.id.startswith(f"azure/{snapshot.scope.subscription_id}/physical/")
    assert network.fact_evidence == {"/address_spaces/0": ["network-address"]}
    assert subnet.fact_evidence == {"/address_prefixes/0": ["subnet-prefix"]}
    assert vm.fact_evidence == {"/vm_size": ["vm-size"]}
    assert snapshot.evidence["vm-size"].value is None
    assert snapshot.evidence["vm-size"].value_status == "present"
    unavailable = snapshot.evidence["vm-unavailable"]
    assert unavailable.value is None
    assert unavailable.value_status == "unresolved"
    assert unavailable.limitation == "Offline fixture does not provide this fact"
    assert "vm-unavailable" not in sum(vm.fact_evidence.values(), [])
    dependency = snapshot.dependencies[0]
    assert dependency.source_id == vm.id
    assert dependency.target.resource_id == subnet.id
    assert dependency.relationship == "attached_to"
    assert dependency.attributes == {"kind": "fixture_attachment"}
    assert dependency.evidence_ids == ["vm-attachment"]
    assert snapshot.evidence["vm-attachment"].resource_id == vm.id
    zero, partial, failed = snapshot.coverage.operations
    assert (zero.status, zero.result_count) == ("success", 0)
    assert (partial.status, partial.result_count) == ("incomplete", 1)
    assert partial.unresolved_values[0].resource_id == vm.id
    assert failed.status == "failed"
    assert failed.result_count is None
    assert failed.failures[0].code == "FixtureUnavailable"
    assert CloudSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize(
    ("defect", "diagnostic"),
    [
        ("resource_scope", "subscription/tenant must match"),
        ("resource_location", "location must match enclosing location"),
        ("duplicate", "duplicate resource"),
        ("citation_owner", "belongs to another resource"),
        ("missing_citation", "unknown evidence"),
        ("absent_fact", "missing key"),
        ("dangling_endpoint", "unknown target_id"),
        ("dependency_owner", "belongs to another resource"),
        ("arm_type", "type must match"),
        ("typed_native_type", "native_type must match"),
        ("evidence_scope", "evidence context must match"),
        ("evidence_location", "evidence context must match"),
        ("evidence_service", "evidence context must match"),
        ("evidence_snapshot", "snapshot_id"),
        ("operation_scope", "scope must match"),
        ("coverage_scope", "scope must match"),
        ("unresolved_owner", "unknown unresolved resource"),
    ],
)
def test_isolated_azure_integrity_defects(azure_data: dict, defect: str, diagnostic: str) -> None:
    network, _, vm = resources(azure_data)
    if defect == "resource_scope":
        vm["identity"]["subscription_id"] = OTHER_SUBSCRIPTION
        vm["identity"]["physical_id"] = vm["identity"]["physical_id"].replace(
            azure_data["scope"]["subscription_id"], OTHER_SUBSCRIPTION
        )
    elif defect == "resource_location":
        vm["identity"]["location"] = "westus"
    elif defect == "duplicate":
        azure_data["regions"]["eastus"]["resources"].append(copy.deepcopy(network))
    elif defect == "citation_owner":
        vm["fact_evidence"]["/vm_size"] = ["network-address"]
    elif defect == "missing_citation":
        vm["evidence_ids"].append("absent")
    elif defect == "absent_fact":
        vm["fact_evidence"]["/attributes/unavailable"] = ["vm-unavailable"]
    elif defect == "dangling_endpoint":
        azure_data["dependencies"][0]["target"]["resource_id"] = "absent"
    elif defect == "dependency_owner":
        azure_data["dependencies"][0]["evidence_ids"] = ["subnet-prefix"]
    elif defect == "arm_type":
        vm["identity"]["physical_id"] = vm["identity"]["physical_id"].replace(
            "/virtualMachines/", "/disks/"
        )
    elif defect == "typed_native_type":
        vm["identity"]["native_type"] = "Microsoft.Compute/disks"
        vm["identity"]["physical_id"] = vm["identity"]["physical_id"].replace(
            "/virtualMachines/", "/disks/"
        )
    elif defect == "evidence_scope":
        azure_data["evidence"]["vm-size"]["scope"]["subscription_id"] = OTHER_SUBSCRIPTION
    elif defect == "evidence_location":
        azure_data["evidence"]["vm-size"]["location"] = "westus"
    elif defect == "evidence_service":
        azure_data["evidence"]["vm-size"]["service"] = "Microsoft.Network"
    elif defect == "evidence_snapshot":
        azure_data["evidence"]["vm-size"]["snapshot_id"] = OTHER_SUBSCRIPTION
    elif defect == "operation_scope":
        azure_data["coverage"]["operations"][0]["scope"]["subscription_id"] = OTHER_SUBSCRIPTION
    elif defect == "coverage_scope":
        azure_data["coverage"]["scope"]["subscription_id"] = OTHER_SUBSCRIPTION
        for operation in azure_data["coverage"]["operations"]:
            operation["scope"]["subscription_id"] = OTHER_SUBSCRIPTION
    else:
        azure_data["coverage"]["operations"][1]["unresolved_values"][0]["resource_id"] = "absent"
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
    assert unknown.platform == "azure"
    assert unknown.native_type == "Microsoft.Storage/storageAccounts"
    assert unknown.identity.scope == snapshot.scope
    assert unknown.attributes == {"fixture_field": "retained"}


@pytest.mark.parametrize("defect", ["scope", "native_type", "platform"])
def test_schema3_unknown_resource_rejects_inconsistent_context(
    azure_data: dict, defect: str
) -> None:
    unknown = unknown_resource(azure_data)
    if defect == "scope":
        unknown["identity"]["subscription_id"] = OTHER_SUBSCRIPTION
        diagnostic = "subscription/tenant must match"
    elif defect == "native_type":
        unknown["native_type"] = "Microsoft.Storage/other"
        diagnostic = "native_type must match"
    else:
        unknown["platform"] = "aws"
        diagnostic = "platform must match identity"
    azure_data["regions"]["eastus"]["resources"].append(unknown)
    with pytest.raises(ValidationError, match=diagnostic):
        CloudSnapshot.model_validate(azure_data)
