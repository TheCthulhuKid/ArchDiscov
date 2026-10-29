import json
from copy import deepcopy

import pytest

from archdiscov.ingestion.cloudformation.mapping import map_resource, service_for_type
from archdiscov.models.architecture import Ec2Instance, Subnet, UnknownResource, Vpc
from archdiscov.models.enums import AwsRegion, AwsService, ResourceType
from archdiscov.models.resource_base import ResourceIdentity


def identity(kind):
    service = service_for_type(kind)
    return ResourceIdentity(
        account_id="123456789012",
        region=AwsRegion.GLOBAL
        if service in (AwsService.IAM, AwsService.S3)
        else AwsRegion.US_EAST_1,
        service=service,
        logical_address="stack/Example",
    )


def resolve(value, pointer):
    for component in pointer.split("/")[1:]:
        component = component.replace("~1", "/").replace("~0", "~")
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def assert_safe_facts(resource, facts, properties):
    for target, source in facts.items():
        assert target.startswith(
            ("/name", "/cidr_blocks/", "/availability_zone", "/instance_type", "/attributes/")
        )
        retained = resolve(resource.model_dump(mode="json"), target)
        original = resolve(properties, source)
        assert not isinstance(retained, (dict, list))
        assert not isinstance(original, (dict, list))
        assert str(retained) == str(original)


@pytest.mark.parametrize(
    "kind,model",
    [
        (ResourceType.VPC, Vpc),
        (ResourceType.SUBNET, Subnet),
        (ResourceType.EC2_INSTANCE, Ec2Instance),
    ],
    ids=["cidrs", "cidrs-and-zone", "instance-type"],
)
def test_core_mapping_is_allowlisted_logical_only_and_cites_safe_leaves(kind, model):
    properties = {
        "CidrBlock": "10.0.0.0/16",
        "Ipv6CidrBlock": "2001:db8::/56",
        "AvailabilityZone": "us-east-1a",
        "InstanceType": "t3.micro",
        "VpcId": "vpc-123",
        "SubnetId": "subnet-123",
        "SecurityGroupIds": ["sg-123", "sg-456"],
        "Tags": [{"Key": "password", "Value": "not-retained"}, {"Key": "Name", "Value": "example"}],
        "UserData": "not-retained",
        "Opaque": {"nested": "not-retained"},
    }
    original = deepcopy(properties)
    resource, facts, limitations = map_resource(str(kind), properties, identity(kind))
    assert isinstance(resource, model)
    assert resource.identity == identity(kind)
    assert resource.identity.physical_id is None
    assert resource.name == "example"
    assert facts["/name"] == "/Tags/1/Value"
    assert resource.attributes["vpc_id"] == "vpc-123"
    assert resource.attributes["subnet_id"] == "subnet-123"
    assert resource.attributes["security_group_ids"] == ["sg-123", "sg-456"]
    assert not limitations
    assert "not-retained" not in resource.model_dump_json()
    assert properties == original
    assert_safe_facts(resource, facts, properties)
    if isinstance(resource, (Vpc, Subnet)):
        assert [str(cidr) for cidr in resource.cidr_blocks] == ["10.0.0.0/16", "2001:db8::/56"]
    if isinstance(resource, Subnet):
        assert resource.availability_zone == "us-east-1a"
    if isinstance(resource, Ec2Instance):
        assert resource.instance_type == "t3.micro"


@pytest.mark.parametrize(
    "kind,properties,attributes",
    [
        (
            ResourceType.LAMBDA_FUNCTION,
            {
                "FunctionName": "function",
                "Runtime": "python3.13",
                "MemorySize": 128,
                "Timeout": 30,
                "Role": "arn:aws:iam::123456789012:role/execution",
            },
            {
                "function_name": "function",
                "runtime": "python3.13",
                "memory_size": 128,
                "timeout": 30,
                "role": "arn:aws:iam::123456789012:role/execution",
            },
        ),
        (
            ResourceType.IAM_ROLE,
            {"RoleName": "role", "Path": "/app/"},
            {"role_name": "role", "path": "/app/"},
        ),
    ],
    ids=["strings-and-integers", "iam-policy-limitation"],
)
def test_arn_models_keep_only_selected_configuration(kind, properties, attributes):
    properties = {
        **properties,
        "Environment": {"Variables": {"PRIVATE": "not-retained"}},
        "Code": {"ZipFile": "not-retained"},
        "ContainerDefinitions": [{"Image": "not-retained"}],
        "MasterUserPassword": "not-retained",
        "AssumeRolePolicyDocument": {"Statement": "not-retained"},
        "Policies": [{"PolicyDocument": "not-retained"}],
    }
    resource, facts, limitations = map_resource(str(kind), properties, identity(kind))
    assert isinstance(resource, UnknownResource)
    assert resource.aws_resource_type == kind
    assert resource.attributes == attributes
    assert resource.identity.physical_id is None
    assert limitations
    assert "not-retained" not in resource.model_dump_json()
    assert_safe_facts(resource, facts, properties)
    if kind == ResourceType.IAM_ROLE:
        assert any("policy" in limitation.lower() for limitation in limitations)


def test_invalid_values_are_omitted_individually_without_losing_valid_facts():
    properties = {
        "CidrBlock": "not-a-network",
        "Ipv6CidrBlock": "2001:db8::/64",
        "AvailabilityZone": {"Ref": "Zone"},
        "VpcId": {"Ref": "Vpc"},
        "SubnetId": "subnet-1",
        "SecurityGroupIds": [False, {"Ref": "Group"}, "sg-valid", ""],
        "Tags": [{"Key": "Name", "Value": {"private": "not-retained"}}],
    }
    resource, facts, limitations = map_resource(
        str(ResourceType.SUBNET), properties, identity(ResourceType.SUBNET)
    )
    assert isinstance(resource, Subnet)
    assert resource.name is None
    assert resource.availability_zone is None
    assert [str(cidr) for cidr in resource.cidr_blocks] == ["2001:db8::/64"]
    assert resource.attributes == {"subnet_id": "subnet-1", "security_group_ids": ["sg-valid"]}
    assert facts["/cidr_blocks/0"] == "/Ipv6CidrBlock"
    assert facts["/attributes/security_group_ids/0"] == "/SecurityGroupIds/2"
    assert limitations
    assert "not-a-network" not in json.dumps(limitations)
    assert_safe_facts(resource, facts, properties)


def test_invalid_scalar_configuration_does_not_abort_or_leak():
    resource, facts, limitations = map_resource(
        str(ResourceType.LAMBDA_FUNCTION),
        {"MemorySize": -1, "Runtime": "python3.13"},
        identity(ResourceType.LAMBDA_FUNCTION),
    )
    assert resource.attributes == {"runtime": "python3.13"}
    assert facts == {"/attributes/runtime": "/Runtime"}
    assert limitations


@pytest.mark.parametrize("value", [None, "::/64"], ids=["not-string", "wrong-version"])
def test_invalid_ipv4_cidr_is_omitted_without_dropping_valid_ipv6(value):
    resource, facts, limitations = map_resource(
        str(ResourceType.VPC),
        {"CidrBlock": value, "Ipv6CidrBlock": "2001:db8::/64"},
        identity(ResourceType.VPC),
    )
    assert [str(cidr) for cidr in resource.cidr_blocks] == ["2001:db8::/64"]
    assert facts == {"/cidr_blocks/0": "/Ipv6CidrBlock"}
    assert limitations == ["Invalid or unresolved CidrBlock omitted."]


@pytest.mark.parametrize(
    "tags",
    ["private", [None]],
    ids=["not-list", "tag-not-mapping"],
)
def test_invalid_tags_do_not_persist_or_abort(tags):
    resource, facts, limitations = map_resource(
        str(ResourceType.EC2_INSTANCE),
        {"Tags": tags, "InstanceType": "t3.micro"},
        identity(ResourceType.EC2_INSTANCE),
    )
    assert resource.name is None
    assert resource.instance_type == "t3.micro"
    assert facts == {"/instance_type": "/InstanceType"}
    assert limitations


def test_network_interface_groups_use_leaf_provenance_not_names_or_opaque_values():
    properties = {
        "GroupSet": ["sg-valid", {"Ref": "LocalGroup"}],
        "SecurityGroups": ["not-an-id"],
        "PrivateIpAddresses": [{"Opaque": "not-retained"}],
    }
    resource, facts, limitations = map_resource(
        str(ResourceType.NETWORK_INTERFACE), properties, identity(ResourceType.NETWORK_INTERFACE)
    )
    assert resource.attributes == {"security_group_ids": ["sg-valid"]}
    assert facts == {"/attributes/security_group_ids/0": "/GroupSet/0"}
    assert limitations
    assert_safe_facts(resource, facts, properties)


def test_inline_rules_are_explicitly_unmapped_and_never_cited():
    resource, facts, limitations = map_resource(
        str(ResourceType.SECURITY_GROUP),
        {"SecurityGroupIngress": [{"FromPort": "not-retained"}]},
        identity(ResourceType.SECURITY_GROUP),
    )
    assert resource.ingress_rules == []
    assert facts == {}
    assert any("rules" in limitation for limitation in limitations)
    assert "not-retained" not in resource.model_dump_json()


def test_malformed_properties_do_not_abort():
    resource, facts, limitations = map_resource(
        str(ResourceType.VPC), "private", identity(ResourceType.VPC)
    )
    assert isinstance(resource, Vpc)
    assert resource.attributes == {}
    assert facts == {}
    assert limitations == ["Invalid or unresolved Properties omitted."]


@pytest.mark.parametrize(
    "kind", ["Custom::Thing", "AWS::IAM::Unrecognized"], ids=["unsupported", "unsupported-iam"]
)
def test_unsupported_type_is_exact_and_does_not_copy_payload(kind):
    resource, facts, limitations = map_resource(
        kind, {"Payload": {"secret": "private"}}, identity(kind)
    )
    assert isinstance(resource, UnknownResource)
    assert resource.aws_resource_type == kind
    assert resource.attributes == {}
    assert facts == {}
    assert limitations


def test_nested_stack_does_not_persist_url_or_parameters():
    kind = "AWS::CloudFormation::Stack"
    resource, facts, limitations = map_resource(
        kind,
        {
            "TemplateURL": "https://private.example/template?token=private",
            "Parameters": {"Private": "not-retained"},
        },
        identity(kind),
    )
    assert isinstance(resource, UnknownResource)
    assert resource.aws_resource_type == kind
    assert resource.attributes == {}
    assert facts == {}
    assert any("contents" in limitation.lower() for limitation in limitations)
    assert "private" not in resource.model_dump_json()


@pytest.mark.parametrize(
    "kind,service",
    [
        (str(ResourceType.VPC), AwsService.EC2),
        ("AWS::IAM::Unrecognized", AwsService.IAM),
        ("AWS::NotSupported::Thing", AwsService.UNKNOWN),
        ("Custom::Thing", AwsService.UNKNOWN),
    ],
    ids=["known-namespace", "unsupported-type-in-known-namespace", "unknown-namespace", "not-aws"],
)
def test_service_for_type(kind, service):
    assert service_for_type(kind) == service
