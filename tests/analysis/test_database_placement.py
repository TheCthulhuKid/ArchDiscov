import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalyzerErrorCode, AwsService
from archdiscov.services import AnalyzerError
from archdiscov.services.database_placement import PortStatus, resolve_database_placement
from tests.support.rds_data import CLUSTER, GROUP, INSTANCE, build, data, ec2_roots, resource_id

RDS = AwsService.RDS
SUBNET_A, SUBNET_B = resource_id("subnet-a"), resource_id("subnet-b")
SG_A, VPC_A = resource_id("sg-a"), resource_id("vpc-a")


def place(arn=INSTANCE, port=5432, payload=None, roots=None):
    index = GraphIndex(build(payload, roots=roots))
    return index, resolve_database_placement(index, resource_id(arn, RDS), port)


def codes(placement):
    return [limitation.code for limitation in placement.limitations]


def payload_with(**changes):
    value = data()
    instance = value["describe_db_instances"]["DBInstances"][0]
    for key, change in changes.items():
        if change is None:
            instance.pop(key, None)
        else:
            instance[key] = change
    return value


# ---- instances ----------------------------------------------------------------------


def test_an_instance_resolves_to_its_stored_vpc_subnets_groups_and_port_with_citations():
    index, placement = place()
    instance = index.get_resource(resource_id(INSTANCE, RDS))
    assert placement.database.id == instance.id
    assert placement.vpc_id == VPC_A
    assert placement.subnet_ids == (SUBNET_A,)
    assert placement.security_group_ids == (SG_A,)
    assert (placement.port_status, placement.recorded_ports) == (PortStatus.MATCH, (5432,))
    assert placement.limitations == ()
    assert set(instance.fact_evidence["/endpoint_port"]) <= set(placement.evidence_ids)
    assert set(instance.fact_evidence["/security_group_ids/0"]) <= set(placement.evidence_ids)
    assert set(instance.fact_evidence["/subnet_ids/0"]) <= set(placement.evidence_ids)
    assert list(placement.evidence_ids) == sorted(placement.evidence_ids)


def test_several_subnets_stay_a_sorted_set():
    value = payload_with()
    group = value["describe_db_instances"]["DBInstances"][0]["DBSubnetGroup"]
    group["Subnets"] = [{"SubnetIdentifier": "subnet-b"}, {"SubnetIdentifier": "subnet-a"}]
    placement = place(payload=value, roots=ec2_roots(subnets=("subnet-a", "subnet-b")))[1]
    assert placement.subnet_ids == (SUBNET_A, SUBNET_B)
    assert placement.vpc_id == VPC_A
    assert placement.limitations == ()


# ---- clusters -----------------------------------------------------------------------


def test_a_cluster_takes_the_placement_of_its_stored_members_and_subnet_group():
    _index, placement = place(CLUSTER)
    assert [member.identifier for member in placement.members] == ["writer"]
    assert placement.subnet_ids == (SUBNET_A,)
    assert placement.security_group_ids == (SG_A,)  # the cluster itself records none
    assert placement.port_status == PortStatus.MATCH
    assert placement.limitations == ()
    assert placement.vpc_id == VPC_A


def test_a_member_that_is_not_stored_is_reported():
    value = data()
    value["describe_db_instances"]["DBInstances"] = []
    placement = place(CLUSTER, payload=value)[1]
    assert placement.members == ()
    assert "database_member_unrecorded" in codes(placement)
    assert "database_security_groups_unrecorded" in codes(placement)
    assert placement.subnet_ids == (SUBNET_A,)  # still resolved through the subnet group


def test_unrecorded_cluster_members_are_reported():
    value = data()
    del value["describe_db_clusters"]["DBClusters"][0]["DBClusterMembers"]
    assert "database_members_unrecorded" in codes(place(CLUSTER, payload=value)[1])


# ---- ports --------------------------------------------------------------------------


def test_a_port_mismatch_is_reported_with_the_recorded_port_and_its_citation():
    index, placement = place(port=3306)
    [limitation] = placement.limitations
    instance = index.get_resource(resource_id(INSTANCE, RDS))
    assert placement.port_status == PortStatus.MISMATCH
    assert limitation.code == "database_port_mismatch"
    assert "5432" in limitation.message
    assert limitation.evidence_ids == instance.fact_evidence["/endpoint_port"]


def test_an_unrecorded_port_is_reported_not_assumed():
    value = payload_with(Endpoint={"Address": "writer.example.rds.amazonaws.com"})
    placement = place(payload=value)[1]
    assert placement.port_status == PortStatus.UNRECORDED
    assert placement.recorded_ports == ()
    assert codes(placement) == ["database_port_unrecorded"]


def test_a_cluster_matches_when_any_recorded_port_matches():
    value = data()
    value["describe_db_clusters"]["DBClusters"][0]["Port"] = 3306
    assert place(CLUSTER, port=3306, payload=value)[1].port_status == PortStatus.MATCH
    placement = place(CLUSTER, port=5432, payload=value)[1]
    assert placement.port_status == PortStatus.MATCH
    assert placement.recorded_ports == (3306, 5432)


# ---- missing placement --------------------------------------------------------------


def test_missing_subnets_and_security_groups_are_limitations_never_absence_of_access():
    value = payload_with(DBSubnetGroup=None, VpcSecurityGroups=[])
    value["describe_db_subnet_groups"]["DBSubnetGroups"] = []
    placement = place(payload=value)[1]
    assert placement.subnet_ids == () and placement.security_group_ids == ()
    assert placement.vpc_id is None
    assert {"database_subnets_unrecorded", "database_security_groups_unrecorded"} <= set(
        codes(placement)
    )


def test_subnets_that_are_not_stored_are_unresolved_targets():
    placement = place(roots=ec2_roots(subnets=(), groups=()))[1]
    assert placement.subnet_ids == () and placement.security_group_ids == ()
    assert codes(placement).count("unresolved_target") >= 2
    assert "database_subnets_unrecorded" in codes(placement)


def test_a_recorded_vpc_that_disagrees_with_the_subnets_is_reported():
    value = payload_with()
    value["describe_db_instances"]["DBInstances"][0]["DBSubnetGroup"]["VpcId"] = "vpc-other"
    placement = place(payload=value)[1]
    assert placement.vpc_id is None
    assert "database_vpc_mismatch" in codes(placement)
    assert placement.subnet_ids == (SUBNET_A,)


def test_subnets_in_different_vpcs_make_the_vpc_ambiguous():
    value = payload_with()
    group = value["describe_db_instances"]["DBInstances"][0]["DBSubnetGroup"]
    group["Subnets"] = [{"SubnetIdentifier": "subnet-a"}, {"SubnetIdentifier": "subnet-b"}]
    roots = [*ec2_roots(), *ec2_roots(subnets=("subnet-b",), groups=(), vpc="vpc-b")]
    placement = place(payload=value, roots=roots)[1]
    assert placement.vpc_id is None
    assert "database_vpc_ambiguous" in codes(placement)


def test_limitations_are_deterministic_and_deduplicated():
    first = place(port=3306, payload=payload_with(VpcSecurityGroups=[]))[1].limitations
    second = place(port=3306, payload=payload_with(VpcSecurityGroups=[]))[1].limitations
    assert first == second
    assert len({(item.code, item.message) for item in first}) == len(first)


# ---- selection ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("physical_id", "code"),
    [
        ("absent", AnalyzerErrorCode.RESOURCE_NOT_FOUND),
        (SUBNET_A, AnalyzerErrorCode.UNSUPPORTED_RESOURCE),
        (resource_id(GROUP, RDS), AnalyzerErrorCode.UNSUPPORTED_RESOURCE),
    ],
    ids=["unknown", "subnet", "subnet-group"],
)
def test_unknown_or_wrongly_typed_selections_are_analyzer_errors(physical_id, code):
    index = GraphIndex(build())
    with pytest.raises(AnalyzerError) as error:
        resolve_database_placement(index, physical_id, 5432)
    assert error.value.code == code
