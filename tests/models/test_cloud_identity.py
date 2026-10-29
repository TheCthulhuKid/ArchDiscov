import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.cloud.identity import (
    AzureResourceIdentity,
    ResourceIdentity,
    SupportedIdentity,
    parse_identity,
)
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import CloudPlatform

SUBSCRIPTION = "12345678-1234-5678-abcd-123456789abc"
OTHER_SUBSCRIPTION = "87654321-4321-8765-dcba-cba987654321"
ARM = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/MyGroup/providers/"
    "Microsoft.Network/virtualNetworks/MyVnet/subnets/MySubnet"
)


def azure(**overrides):
    return AzureResourceIdentity.model_validate(
        {
            "subscription_id": SUBSCRIPTION,
            "resource_group": "MyGroup",
            "location": "eastus",
            "service": "Microsoft.Network",
            "native_type": "Microsoft.Network/virtualNetworks/subnets",
            "physical_id": ARM,
            **overrides,
        }
    )


def test_aws_wire_and_stable_id_are_unchanged():
    data = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "service": "ec2",
        "physical_id": "i-AbC/123",
        "logical_address": None,
    }
    identity = parse_identity(data)
    assert isinstance(identity, ResourceIdentity)
    assert identity.platform == CloudPlatform.AWS
    assert identity.model_dump(mode="json") == data
    assert identity.stable_id == "123456789012/us-east-1/ec2/physical/i-AbC%2F123"
    assert identity.stable_id != parse_identity({**data, "physical_id": "i-abc/123"}).stable_id
    assert parse_identity(identity) is identity
    assert TypeAdapter(SupportedIdentity).validate_json(identity.model_dump_json()) == identity


def test_aws_logical_identity_is_unchanged():
    identity = ResourceIdentity(
        account_id="123456789012", region="global", service="iam", logical_address="aws_iam_role.x"
    )
    assert identity.stable_id == "123456789012/global/iam/logical/aws_iam_role.x"
    both = ResourceIdentity(**{**identity.model_dump(), "physical_id": "role-x"})
    assert both.stable_id == "123456789012/global/iam/physical/role-x"
    with pytest.raises(ValidationError):
        ResourceIdentity(account_id="123456789012", region="global", service="iam")


def test_azure_scope_uuids_are_strict_and_canonical():
    scope = AzureScope(subscription_id=SUBSCRIPTION.upper(), tenant_id=OTHER_SUBSCRIPTION.upper())
    assert scope.subscription_id == SUBSCRIPTION
    assert scope.tenant_id == OTHER_SUBSCRIPTION
    with pytest.raises(ValidationError):
        AzureScope(subscription_id=f" {SUBSCRIPTION}")


def test_azure_case_equivalence_and_physical_location_independence():
    original = azure()
    equivalent = azure(
        subscription_id=SUBSCRIPTION.upper(),
        resource_group="mygroup",
        location="westus2",
        service="microsoft.network",
        native_type="microsoft.network/virtualnetworks/subnets",
        physical_id=ARM.upper(),
    )
    assert original.physical_id == ARM
    assert equivalent.physical_id == ARM.upper()
    assert original.canonical_arm_id == ARM.lower()
    assert original.stable_id == equivalent.stable_id
    assert original.stable_id.startswith(f"azure/{SUBSCRIPTION}/physical/")
    assert original.scope == AzureScope(subscription_id=SUBSCRIPTION)
    assert parse_identity(original.model_dump(mode="json")) == original
    assert parse_identity(original) is original
    assert original.model_dump(mode="json")["platform"] == "azure"
    other = azure(
        subscription_id=OTHER_SUBSCRIPTION,
        physical_id=ARM.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION),
    )
    assert original.stable_id != other.stable_id


def test_logical_identity_has_distinct_complete_namespace():
    logical = azure(physical_id=None, logical_address="azurerm_subnet.example")
    assert "/logical/" in logical.stable_id
    assert logical.stable_id != azure(logical_address="azurerm_subnet.example").stable_id
    for overrides in (
        {"subscription_id": OTHER_SUBSCRIPTION},
        {"resource_group": "OtherGroup"},
        {"location": "westus"},
        {"native_type": "Microsoft.Network/virtualNetworks"},
        {"service": "Microsoft.Compute", "native_type": "Microsoft.Compute/virtualMachines"},
        {"logical_address": "azurerm_subnet.other"},
    ):
        assert (
            logical.stable_id
            != AzureResourceIdentity.model_validate({**logical.model_dump(), **overrides}).stable_id
        )
    assert (
        logical.stable_id
        == azure(
            physical_id=None,
            logical_address="azurerm_subnet.example",
            resource_group="mygroup",
            native_type="microsoft.network/virtualnetworks/subnets",
            service="microsoft.network",
        ).stable_id
    )
    assert (
        logical.stable_id
        != azure(physical_id=None, logical_address="azurerm_subnet.Example").stable_id
    )


def test_subscription_wide_resource():
    identity = azure(
        resource_group=None,
        location=None,
        native_type="Microsoft.Network/virtualNetworks",
        physical_id=f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Network/virtualNetworks/test",
    )
    assert identity.resource_group is None
    assert identity.location is None
    assert azure(tenant_id=OTHER_SUBSCRIPTION).scope == AzureScope(
        subscription_id=SUBSCRIPTION, tenant_id=OTHER_SUBSCRIPTION
    )
    logical = AzureResourceIdentity(
        subscription_id=SUBSCRIPTION,
        service="Microsoft.Compute",
        native_type="Microsoft.Compute/virtualMachines",
        logical_address="azurerm_linux_virtual_machine.example",
    )
    assert logical.canonical_arm_id is None
    assert logical.resource_group is None
    assert logical.location is None


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"physical_id": ARM + "?query=1"}, "subscription-scoped ARM resource ID"),
        (
            {"physical_id": ARM.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION)},
            "ARM subscription must match",
        ),
        ({"resource_group": " MyGroup"}, "without surrounding whitespace"),
        ({"native_type": "Microsoft.Compute/virtualMachines"}, "namespace must match service"),
    ],
    ids=["not-arm", "arm-subscription", "padded-text", "namespace-mismatch"],
)
def test_invalid_azure_identity_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        azure(**overrides)
