import json
from copy import deepcopy

import pytest

from archdiscov.ingestion.terraform.catalog import RESOURCE_MAPPINGS, service_for_type
from archdiscov.ingestion.terraform.mapping import map_resource
from archdiscov.models.architecture import Ec2Instance, Subnet, UnknownResource, Vpc
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.network import (
    SecurityGroup,
)
from tests.support.terraform_data import identity


def resolve(value, pointer):
    for component in pointer.split("/")[1:]:
        component = component.replace("~1", "/").replace("~0", "~")
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def test_catalog_keeps_model_discriminators_and_unknown_summaries_consistent():
    for kind, mapping in RESOURCE_MAPPINGS.items():
        assert service_for_type(kind) == mapping.service
        if mapping.model is not None:
            assert mapping.model.model_fields["resource_type"].default == mapping.aws_type
            continue
        resource, _, limitations = map_resource(kind, {}, identity(kind))
        assert isinstance(resource, UnknownResource)
        assert resource.aws_resource_type == mapping.aws_type
        assert limitations


@pytest.mark.parametrize(
    "kind,model",
    [
        ("aws_vpc", Vpc),
        ("aws_subnet", Subnet),
        ("aws_instance", Ec2Instance),
        ("aws_security_group", SecurityGroup),
    ],
    ids=["cidrs", "cidrs-and-zone", "instance-type", "inline-collections"],
)
def test_core_models_and_fact_pointers(kind, model):
    values = {
        "cidr_block": "10.0.0.0/16",
        "ipv6_cidr_block": "2001:db8::/56",
        "availability_zone": "us-east-1a",
        "instance_type": "t3.micro",
        "tags": {"Name": "example", "a/b~c": "tag-value", "password": "[REDACTED]"},
        "vpc_id": "vpc-1",
        "subnet_id": "subnet-1",
        "vpc_security_group_ids": ["sg-1", "sg-2"],
        "iam_instance_profile": "profile-name",
        "user_data": "do-not-copy",
        "provider_private_data": {"secret": "do-not-copy"},
        "ingress": [],
        "egress": [],
        "route": [],
    }
    original = deepcopy(values)
    resource, facts, limitations = map_resource(kind, values, identity(kind))
    assert isinstance(resource, model)
    assert not limitations
    assert resource.id == identity(kind).stable_id
    assert resource.name == "example"
    assert resource.attributes["security_group_ids"] == ["sg-1", "sg-2"]
    assert resource.attributes["iam_instance_profile"] == "profile-name"
    assert "do-not-copy" not in resource.model_dump_json()
    assert values == original
    record = {"type": kind, "values": values}
    for target, source in facts.items():
        assert resolve(resource.model_dump(mode="json"), target) == resolve(record, source) or (
            target == "/resource_type"
        )
    if isinstance(resource, (Vpc, Subnet)):
        assert [str(block) for block in resource.cidr_blocks] == [
            "10.0.0.0/16",
            "2001:db8::/56",
        ]
    if isinstance(resource, Subnet):
        assert resource.availability_zone == "us-east-1a"
    if isinstance(resource, Ec2Instance):
        assert resource.instance_type == "t3.micro"


def test_iam_uses_source_arn_not_physical_id_and_omits_policy_payloads():
    kind = "aws_iam_role"
    arn = "arn:aws:iam::123456789012:role/example"
    resource, facts, limitations = map_resource(
        kind,
        {"arn": arn, "name": "example", "policy": "do-not-copy"},
        identity(kind, physical_id="example"),
    )
    assert isinstance(resource, IamRole)
    assert resource.arn == arn
    assert resource.identity.physical_id == "example"
    assert facts["/arn"] == "/values/arn"
    assert limitations
    assert "do-not-copy" not in resource.model_dump_json() + json.dumps(limitations)


@pytest.mark.parametrize(
    "kind,aws_type,service",
    [
        ("aws_lambda_function", "AWS::Lambda::Function", "lambda"),
        ("aws_future_service", "aws_future_service", "unknown"),
    ],
    ids=["catalogued-untyped", "not-catalogued"],
)
def test_unsupported_resources_retain_only_safe_type_summary(kind, aws_type, service):
    resource, facts, limitations = map_resource(
        kind,
        {
            "name": "do-not-copy",
            "tags": {"Name": "do-not-copy"},
            "environment": {"plain_name": "do-not-copy"},
        },
        identity(kind),
    )
    assert isinstance(resource, UnknownResource)
    assert resource.aws_resource_type == aws_type
    assert resource.attributes == {"terraform_type": kind}
    assert resource.name is None
    assert all(pointer == "/type" for pointer in facts.values())
    assert service_for_type(kind).value == service
    assert limitations
    assert "do-not-copy" not in resource.model_dump_json()


def test_invalid_cidr_is_omitted_without_losing_valid_siblings_or_leaking_errors():
    resource, facts, limitations = map_resource(
        "aws_subnet",
        {
            "cidr_block": "secret-invalid-value",
            "ipv6_cidr_block": "2001:db8::/64",
            "availability_zone": {"nested": "secret-invalid-value"},
        },
        identity(),
    )
    assert [str(block) for block in resource.cidr_blocks] == ["2001:db8::/64"]
    assert facts["/cidr_blocks/0"] == "/values/ipv6_cidr_block"
    assert "/availability_zone" not in facts
    assert len(limitations) == 2
    assert "secret-invalid-value" not in resource.model_dump_json() + json.dumps(limitations)


def test_redacted_structures_are_omitted_but_redacted_tag_values_are_retained():
    resource, facts, limitations = map_resource(
        "aws_instance",
        {
            "tags": "[REDACTED]",
            "name": "[REDACTED]",
            "security_groups": "[REDACTED]",
            "instance_type": "[REDACTED]",
        },
        identity(),
    )
    assert resource.name is None
    assert resource.instance_type is None
    assert resource.attributes == {"terraform_type": "aws_instance"}
    assert all(source == "/type" for source in facts.values())
    assert len(limitations) == 4
    resource, facts, _ = map_resource(
        "aws_vpc",
        {"tags": {"Name": "[REDACTED]", "secret": "[REDACTED]"}, "name": "fallback"},
        identity(),
    )
    assert resource.name == "fallback"
    assert resource.attributes["tags"]["secret"] == "[REDACTED]"
    assert facts["/name"] == "/values/name"


def test_deferred_rules_are_not_copied_and_absent_rules_are_unavailable_not_empty():
    kind = "aws_security_group"
    values = {"ingress": [{"description": "secret-rule"}]}
    resource, facts, limitations = map_resource(kind, values, identity(kind))
    assert limitations
    assert set(facts.values()) == {"/type"}
    assert "secret-rule" not in resource.model_dump_json() + json.dumps(limitations)
    resource, facts, limitations = map_resource(kind, {}, identity(kind))
    assert resource.attributes["unavailable_fields"] == ["ingress_rules", "egress_rules"]
    assert "/ingress_rules" not in facts and "/egress_rules" not in facts
    assert limitations


@pytest.mark.parametrize(
    "primary,expected,source,has_limitations",
    [
        (None, None, "/values/vpc_security_group_ids", False),
        ([], [], "/values/vpc_security_group_ids", False),
        (["sg-primary"], ["sg-primary"], "/values/vpc_security_group_ids/0", False),
        ("[REDACTED]", ["sg-fallback"], "/values/security_groups/0", True),
    ],
    ids=["absent", "empty", "primary", "redacted-falls-back"],
)
def test_security_group_source_precedence_and_fallback(primary, expected, source, has_limitations):
    values = {"vpc_security_group_ids": primary, "security_groups": ["sg-fallback"]}
    original = deepcopy(values)
    resource, facts, limitations = map_resource("aws_instance", values, identity("aws_instance"))
    assert resource.attributes["security_group_ids"] == expected
    pointer = "/attributes/security_group_ids" + ("/0" if expected else "")
    assert facts[pointer] == source
    assert bool(limitations) == has_limitations
    assert values == original
