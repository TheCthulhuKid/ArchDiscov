import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import Vpc
from archdiscov.models.resources.azure import AzureVirtualMachine

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


def azure_identity():
    return {
        "platform": "azure",
        "subscription_id": SUBSCRIPTION,
        "resource_group": "example",
        "location": "eastus",
        "service": "Microsoft.Network",
        "native_type": "Microsoft.Network/virtualNetworks",
        "physical_id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example/providers"
            "/Microsoft.Network/virtualNetworks/network"
        ),
    }


def test_aws_resource_rejects_azure_identity():
    with pytest.raises(ValidationError, match="platform must match"):
        Vpc(identity=azure_identity())


def test_azure_resource_rejects_wrong_native_type():
    with pytest.raises(ValidationError, match="native_type must match"):
        AzureVirtualMachine(identity=azure_identity())
