from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.cloud_identity import (
    AzureResourceIdentity,
    ResourceIdentity,
    SupportedIdentity,
    parse_identity,
)
from archdiscov.models.cloud_scope import AwsScope, AzureScope, SupportedScope
from archdiscov.models.enums import AwsPartition, CloudPlatform, ResourceType

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
    with pytest.raises(ValidationError):
        ResourceIdentity.model_validate({**data, "unexpected": True})


def test_aws_logical_identity_is_unchanged():
    identity = ResourceIdentity(
        account_id="123456789012", region="global", service="iam", logical_address="aws_iam_role.x"
    )
    assert identity.stable_id == "123456789012/global/iam/logical/aws_iam_role.x"
    with pytest.raises(ValidationError):
        ResourceIdentity(account_id="123456789012", region="global", service="iam")


def test_scopes_and_enum_contract():
    aws = AwsScope(account_id="123456789012")
    assert aws.platform == CloudPlatform.AWS
    assert aws.partition == AwsPartition.AWS
    assert {partition.value for partition in AwsPartition} == {"aws", "aws-cn", "aws-us-gov"}
    scope = AzureScope(subscription_id=SUBSCRIPTION.upper(), tenant_id=OTHER_SUBSCRIPTION.upper())
    assert scope.subscription_id == SUBSCRIPTION
    assert scope.tenant_id == OTHER_SUBSCRIPTION
    for item in (aws, scope):
        assert TypeAdapter(SupportedScope).validate_json(item.model_dump_json()) == item
    assert ResourceType.AZURE_VIRTUAL_NETWORK == "Microsoft.Network/virtualNetworks"
    assert ResourceType.AZURE_SUBNET == "Microsoft.Network/virtualNetworks/subnets"
    assert ResourceType.AZURE_VIRTUAL_MACHINE == "Microsoft.Compute/virtualMachines"
    assert ResourceType.CLOUD_UNKNOWN == "cloud_unknown"


@pytest.mark.parametrize(
    "scope_type,data",
    [
        (AwsScope, {"account_id": "123"}),
        (AwsScope, {"account_id": "123456789012", "partition": "invalid"}),
        (AwsScope, {"account_id": "123456789012", "platform": "azure"}),
        (AwsScope, {"account_id": "123456789012", "unexpected": True}),
        (AzureScope, {"subscription_id": SUBSCRIPTION, "platform": "aws"}),
        (AzureScope, {"subscription_id": SUBSCRIPTION, "unexpected": True}),
    ],
)
def test_invalid_scopes_are_rejected(scope_type, data):
    with pytest.raises(ValidationError):
        scope_type.model_validate(data)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        SUBSCRIPTION.replace("-", ""),
        f"{{{SUBSCRIPTION}}}",
        f" {SUBSCRIPTION}",
        f"{SUBSCRIPTION} ",
        UUID(SUBSCRIPTION),
        123,
    ],
)
@pytest.mark.parametrize("field", ["subscription_id", "tenant_id"])
def test_scopes_require_strict_uuid_strings(field, value):
    with pytest.raises(ValidationError):
        AzureScope.model_validate({"subscription_id": SUBSCRIPTION, field: value})
    with pytest.raises(ValidationError):
        azure(**{field: value})


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
    "arm",
    [
        ARM + "/",
        " " + ARM,
        ARM + " ",
        ARM.replace("/subscriptions/", "/tenants/"),
        ARM.replace(SUBSCRIPTION, OTHER_SUBSCRIPTION),
        ARM.replace(SUBSCRIPTION, "invalid"),
        ARM.replace("MyGroup", "other"),
        ARM.replace("/providers/", "/provider/"),
        ARM.replace("Microsoft.Network", "Microsoft.Compute"),
        ARM.replace("virtualNetworks", "virtualMachines"),
        ARM.rsplit("/", 1)[0],
        ARM.replace("/MyVnet/", "//"),
        ARM.replace("/subnets/", "/subnets/extra/"),
        ARM + "?query=1",
        ARM + "#fragment",
        ARM.replace("subnets", "bad type"),
    ],
)
def test_malformed_or_inconsistent_arm_is_rejected(arm):
    with pytest.raises(ValidationError):
        azure(physical_id=arm)


@pytest.mark.parametrize(
    "overrides",
    [
        {"physical_id": None},
        {"resource_group": None},
        {"resource_group": " MyGroup"},
        {"location": " "},
        {"location": 1},
        {"service": "ec2"},
        {"service": " Microsoft.Network"},
        {"native_type": "Microsoft.Network"},
        {"native_type": "Microsoft.Compute/virtualMachines"},
        {"logical_address": " "},
        {"platform": "aws"},
        {"unexpected": True},
    ],
)
def test_invalid_azure_identity_is_rejected(overrides):
    with pytest.raises(ValidationError):
        azure(**overrides)


def test_unknown_platform_is_rejected():
    with pytest.raises(ValidationError):
        parse_identity({"platform": "gcp"})
