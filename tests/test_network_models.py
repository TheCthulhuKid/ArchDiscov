from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.aws import require_allowed
from archdiscov.ingestion.operations import ALLOWED_OPERATIONS, Ec2Operation
from archdiscov.models.enums import AwsService, ResourceType, RouteTargetKind
from archdiscov.models.network import (
    InternetGateway,
    NetworkAcl,
    NetworkInterface,
    RouteTable,
    SecurityGroup,
)
from archdiscov.services import load_snapshot, save_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"
IDENTITY = {"account_id": "123456789012", "region": "eu-west-1", "service": "ec2"}


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def test_network_fixture_round_trips_with_typed_children(tmp_path):
    snapshot = load_snapshot(FIXTURES / "network.json")
    by_physical = {
        r.identity.physical_id: r for r in flatten(snapshot.regions["eu-west-1"].resources)
    }
    assert isinstance(by_physical["rtb-main"], RouteTable)
    assert isinstance(by_physical["igw-main"], InternetGateway)
    assert isinstance(by_physical["acl-main"], NetworkAcl)
    assert isinstance(by_physical["sg-web"], SecurityGroup)
    assert isinstance(by_physical["eni-web"], NetworkInterface)
    assert by_physical["rtb-main"].routes[1].target_kind is RouteTargetKind.INTERNET_GATEWAY
    assert by_physical["sg-web"].ingress_rules[1].referenced_group_id == "sg-db"
    assert by_physical["eni-web"].private_ip_addresses[0].address == "10.0.1.5"

    saved = save_snapshot(snapshot, tmp_path / "out.json")
    assert load_snapshot(saved) == snapshot


def test_rule_and_route_citations_resolve_to_specific_entries():
    snapshot = load_snapshot(FIXTURES / "network.json")
    group = next(
        r
        for r in flatten(snapshot.regions["eu-west-1"].resources)
        if r.identity.physical_id == "sg-web"
    )
    cited = snapshot.evidence[group.fact_evidence["/ingress_rules/0"][0]]
    assert cited.configuration_path == "SecurityGroups[0].IpPermissions[0]"


def test_schema_one_fixtures_still_load():
    for name in ("hierarchy.json", "aws.json"):
        assert load_snapshot(FIXTURES / name).schema_version == "1"


@pytest.mark.parametrize(
    "fields",
    [
        {"ingress_rules": [{"direction": "ingress", "from_port": 70000}]},
        {"ingress_rules": [{"direction": "ingress", "cidr": "not-a-cidr"}]},
        {"ingress_rules": [{"direction": "sideways"}]},
    ],
)
def test_invalid_rule_values_are_rejected(fields):
    with pytest.raises(ValidationError):
        SecurityGroup(identity={**IDENTITY, "physical_id": "sg-1"}, **fields)


def test_resource_types_match_aws_names():
    group = SecurityGroup(identity={**IDENTITY, "physical_id": "sg-1"})
    assert group.resource_type is ResourceType.SECURITY_GROUP
    assert group.resource_type.value == "AWS::EC2::SecurityGroup"


def test_new_operations_are_allowlisted_and_others_rejected():
    for operation in Ec2Operation:
        assert (AwsService.EC2, operation.value) in ALLOWED_OPERATIONS
        require_allowed(AwsService.EC2, operation.value)
    with pytest.raises(IngestionError) as exc:
        require_allowed(AwsService.EC2, "RunInstances")
    assert exc.value.code == "operation_not_allowed"
