from archdiscov.services import collect_snapshots
from tests.support.aws_fakes import request
from tests.support.ec2_data import VPC, ec2_context

ACCOUNT = "123456789012"


def test_collects_vpc_subnet_instance_hierarchy_with_evidence():
    (snapshot,) = collect_snapshots(request(ACCOUNT), context=ec2_context())
    roots = snapshot.regions["us-east-1"].resources
    vpc = next(r for r in roots if r.identity.physical_id == VPC)
    assert vpc.name == "main"
    assert [str(c) for c in vpc.cidr_blocks] == ["10.0.0.0/16", "10.1.0.0/16"]
    (subnet,) = vpc.children
    assert subnet.availability_zone == "us-east-1a"
    assert [str(c) for c in subnet.cidr_blocks] == ["10.0.1.0/24"]
    (instance,) = subnet.children
    assert instance.instance_type == "t3.micro"
    assert instance.name == "web"
    assert instance.attributes["security_group_ids"] == ["sg-1"]

    cited = snapshot.evidence[instance.fact_evidence["/instance_type"][0]]
    assert cited.resource_id == instance.id
    assert cited.locator == "ec2:DescribeInstances"
    assert cited.configuration_path == "Reservations[0].Instances[0].InstanceType"
    assert cited.value == "t3.micro"
    # Evidence IDs stay stable for snapshots saved by earlier versions.
    assert instance.evidence_ids[0] == "us-east-1/DescribeInstances/i-1"
    whole = snapshot.evidence[instance.evidence_ids[0]]
    assert whole.value["LaunchTime"] == "2025-01-01T00:00:00+00:00"
    assert whole.value_status == "redacted"
    assert whole.redacted_paths == ["/Tags/1/Value"]
    assert "hunter2" not in snapshot.model_dump_json()
    assert snapshot.coverage.account_wide is False

    # Unavailable parents are recorded, not synthesized.
    by_physical = {r.identity.physical_id: r for r in roots}
    assert by_physical["subnet-orphan"].attributes["unresolved_parent"]["id"] == "vpc-missing"
    assert by_physical["i-nosubnet"].attributes["unresolved_parent"]["kind"] == "subnet"
    assert not any(r.identity.physical_id in ("vpc-missing", "subnet-gone") for r in roots)
