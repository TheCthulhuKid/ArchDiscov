import re
from ipaddress import IPv4Address

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AccessSourceKind, AwsService
from archdiscov.services.database_placement import resolve_database_placement
from archdiscov.services.database_sources import SourceStatus, discover_sources
from tests.support.rds_data import INSTANCE, data
from tests.support.source_data import EC2_ITEMS, build, rid

K = AccessSourceKind
SUBNET_A, SUBNET_B, SG_APP = rid("subnet-a"), rid("subnet-b"), rid("sg-app")
BOTH = {"task": {}, "function": {}}


def discover(limit=100, rds=None, **sources):
    index = GraphIndex(build(rds=rds, **sources))
    placement = resolve_database_placement(index, rid(INSTANCE, AwsService.RDS), 5432)
    return index, discover_sources(index, placement, limit)


def by_name(discovery):
    return {re.split(r"[:/]", c.resource.identity.physical_id)[-1]: c for c in discovery.candidates}


def codes(candidate):
    return [item.code for item in candidate.limitations]


def ec2_without_interfaces():
    items = dict(EC2_ITEMS)
    del items[next(key for key in items if key.value == "DescribeNetworkInterfaces")]
    return items


# ---- each source kind ---------------------------------------------------------------


def test_each_source_kind_is_placed_with_subnet_groups_and_addresses():
    _, discovery = discover(**BOTH)
    found = by_name(discovery)
    plain = found["i-plain"]
    assert (plain.kind, plain.status) == (K.EC2_INSTANCE, SourceStatus.PLACED)
    [interface] = plain.interfaces
    assert (interface.subnet_id, interface.security_group_ids) == (SUBNET_A, (SG_APP,))
    assert interface.addresses == (IPv4Address("10.0.1.20"),)
    task = found["0123456789abcdef"]
    assert (task.kind, task.status) == (K.ECS_TASK, SourceStatus.PLACED)
    assert task.interfaces[0].addresses == (IPv4Address("10.0.1.30"),)
    function = found["worker"]
    assert (function.kind, function.status) == (K.LAMBDA_FUNCTION, SourceStatus.PLACED)
    assert function.interfaces[0].subnet_id == SUBNET_B
    assert function.interfaces[0].addresses == ()
    assert "source_address_unrecorded" in codes(function)


def test_an_instance_with_several_interfaces_keeps_each_subnet_and_address():
    _, discovery = discover()
    multi = by_name(discovery)["i-multi"]
    assert [(i.subnet_id, i.addresses) for i in multi.interfaces] == [
        (SUBNET_A, (IPv4Address("10.0.1.10"),)),
        (SUBNET_B, (IPv4Address("10.0.2.10"),)),
    ]
    assert multi.limitations == ()


def test_an_instance_without_interface_records_uses_its_own_facts_and_address():
    _, discovery = discover(ec2=ec2_without_interfaces())
    found = by_name(discovery)
    [interface] = found["i-plain"].interfaces
    assert interface.addresses == (IPv4Address("10.0.1.20"),)
    assert found["i-plain"].limitations == ()
    assert "source_address_unrecorded" in codes(found["i-multi"])


def test_candidates_cite_their_placement_facts():
    index, discovery = discover(**BOTH)
    found = by_name(discovery)
    multi = index.get_resource(found["i-multi"].resource.id)
    eni = index.get_resource(rid("eni-1"))
    assert set(eni.fact_evidence["/private_ip_addresses"]) <= set(found["i-multi"].evidence_ids)
    task = index.get_resource(found["0123456789abcdef"].resource.id)
    assert set(task.fact_evidence["/subnet_ids/0"]) <= set(found["0123456789abcdef"].evidence_ids)
    assert multi.id == found["i-multi"].resource.id


# ---- missing or partial placement ---------------------------------------------------


def test_sources_without_a_security_group_are_incomplete_candidates():
    _, discovery = discover()
    nosg = by_name(discovery)["i-nosg"]
    assert nosg.status == SourceStatus.INCOMPLETE
    assert nosg.interfaces == ()
    assert "source_security_groups_unrecorded" in codes(nosg)


@pytest.mark.parametrize(
    ("source", "kwargs", "code"),
    [
        ("task", {"mode": "bridge"}, "source_not_awsvpc"),
        ("task", {"subnets": ()}, "source_subnets_unrecorded"),
        ("task", {"groups": ()}, "source_security_groups_unrecorded"),
        ("function", {"subnets": ()}, "source_not_in_vpc"),
        ("function", {"groups": ()}, "source_security_groups_unrecorded"),
    ],
)
def test_unplaceable_tasks_and_functions_stay_incomplete_candidates(source, kwargs, code):
    _, discovery = discover(**{source: kwargs})
    candidate = next(c for c in discovery.candidates if c.kind != K.EC2_INSTANCE)
    assert candidate.status == SourceStatus.INCOMPLETE
    assert candidate.interfaces == ()
    assert code in codes(candidate)


def test_a_task_without_interface_records_has_no_address():
    _, discovery = discover(task={"enis": ()})
    task = next(c for c in discovery.candidates if c.kind == K.ECS_TASK)
    assert task.status == SourceStatus.PLACED
    assert task.interfaces[0].addresses == ()
    assert "source_address_unrecorded" in codes(task)


# ---- other VPCs ---------------------------------------------------------------------


def test_sources_in_other_vpcs_are_counted_not_listed():
    _, discovery = discover(
        function={"vpc": "vpc-b", "subnets": ("subnet-x",), "groups": ("sg-x",)}
    )
    assert rid("i-other") in discovery.other_vpc_ids
    assert any("worker" in item for item in discovery.other_vpc_ids)
    assert "i-other" not in by_name(discovery)
    assert [item.code for item in discovery.limitations] == ["sources_other_vpc"]
    assert "2 compute" in discovery.limitations[0].message


def test_discovery_needs_the_database_vpc():
    value = data()
    value["describe_db_instances"]["DBInstances"][0].pop("DBSubnetGroup")
    value["describe_db_subnet_groups"]["DBSubnetGroups"] = []
    _, discovery = discover(rds=value)
    assert discovery.candidates == ()
    assert discovery.total == 0
    assert [item.code for item in discovery.limitations] == ["sources_unfiltered"]


# ---- ordering and bounds ------------------------------------------------------------


def test_candidates_are_ordered_by_status_kind_and_id_and_deterministic():
    _, first = discover(**BOTH)
    _, second = discover(**BOTH)
    assert first == second
    keys = [(c.status, c.kind, c.resource.id) for c in first.candidates]
    order = list(SourceStatus), list(K)
    assert keys == sorted(keys, key=lambda k: (order[0].index(k[0]), order[1].index(k[1]), k[2]))
    assert first.candidates[-1].status == SourceStatus.INCOMPLETE


def test_discovery_is_bounded_with_explicit_truncation():
    _, full = discover(**BOTH)
    _, bounded = discover(limit=2, **BOTH)
    assert full.total == len(full.candidates) == 5 and not full.truncated
    assert bounded.total == 5 and len(bounded.candidates) == 2 and bounded.truncated
    assert bounded.candidates == full.candidates[:2]
