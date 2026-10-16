import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import Vpc
from archdiscov.models.cloud.identity import ResourceIdentity
from archdiscov.models.resources.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def azure_identity(native_type="Microsoft.Network/virtualNetworks", name="network"):
    namespace, *types = native_type.split("/")
    suffix = "/".join(part for pair in zip(types, name.split("/"), strict=True) for part in pair)
    return {
        "platform": "azure",
        "subscription_id": SUBSCRIPTION,
        "resource_group": "example",
        "location": "eastus",
        "service": namespace,
        "native_type": native_type,
        "physical_id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example/providers/{namespace}/{suffix}"
        ),
    }


def test_azure_recursive_resources_and_fields():
    vm = AzureVirtualMachine(
        identity=azure_identity("Microsoft.Compute/virtualMachines", "vm"), vm_size="Standard_B1s"
    )
    subnet = AzureSubnet(
        identity=azure_identity("Microsoft.Network/virtualNetworks/subnets", "network/subnet"),
        address_prefixes=["10.0.0.0/24"],
        children=[vm],
    )
    network = AzureVirtualNetwork(
        identity=azure_identity(), address_spaces=["10.0.0.0/16"], children=[subnet]
    )
    restored = AzureVirtualNetwork.model_validate_json(network.model_dump_json())
    assert isinstance(restored.children[0], AzureSubnet)
    assert isinstance(restored.children[0].children[0], AzureVirtualMachine)
    assert restored.children[0].children[0].vm_size == "Standard_B1s"
    assert restored == network


def test_aws_resource_rejects_azure_identity():
    with pytest.raises(ValidationError, match="platform must match"):
        Vpc(identity=azure_identity())


def test_azure_resource_rejects_aws_identity():
    identity = ResourceIdentity(
        account_id="123456789012", region="eu-west-1", service="ec2", physical_id="vpc-1"
    )
    with pytest.raises(ValidationError) as error:
        AzureVirtualNetwork(identity=identity)
    assert any(
        item["loc"] == ("identity",) and item["type"] == "model_type"
        for item in error.value.errors()
    )


def test_azure_resource_rejects_wrong_native_type():
    with pytest.raises(ValidationError, match="native_type must match"):
        AzureVirtualMachine(identity=azure_identity())
