import re

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AccessElement, AwsService, NetworkDecision
from archdiscov.services.database_access_paths import evaluate_candidate
from archdiscov.services.database_placement import resolve_database_placement
from archdiscov.services.database_sources import discover_sources
from tests.support.rds_data import INSTANCE, data
from tests.support.source_data import acl_entry, build, ec2_items, rid, rule

D = NetworkDecision
E = AccessElement
ALLOW_ALL = rule("sg-app", egress=True, protocol="-1", cidr="0.0.0.0/0")
DB_EGRESS = rule("sg-a", egress=True, protocol="-1", cidr="0.0.0.0/0")
REFERENCE = rule("sg-a", referenced="sg-app")
OPEN = [acl_entry(100), acl_entry(100, egress=True)]


def evaluate(function=None, rds=None, **network):
    index = GraphIndex(build(ec2=ec2_items(**network), function=function, rds=rds))
    placement = resolve_database_placement(index, rid(INSTANCE, AwsService.RDS), 5432)
    discovery = discover_sources(index, placement, 100)
    return {
        re.split(r"[:/]", c.resource.identity.physical_id)[-1]: evaluate_candidate(
            index, placement, c, 5432
        )
        for c in discovery.candidates
    }


def decisions(evaluation):
    return {item.element: item.decision for item in evaluation.elements}


def rds(*, subnets=None, groups=None):
    """The RDS data with the instance's subnet-group subnets or its security groups replaced."""
    value = data()
    instance = value["describe_db_instances"]["DBInstances"][0]
    if subnets is not None:
        instance["DBSubnetGroup"]["Subnets"] = [{"SubnetIdentifier": s} for s in subnets]
    if groups is not None:
        instance["VpcSecurityGroups"] = groups
    return value


def test_an_instance_in_the_database_subnet_is_allowed_through_a_group_reference():
    path = evaluate()["i-plain"]
    assert path.decision == D.ALLOW
    # Traffic inside one subnet is not filtered by its network ACL.
    assert decisions(path) == {
        E.SOURCE_EGRESS: D.ALLOW,
        E.DATABASE_INGRESS: D.ALLOW,
        E.ROUTE: D.ALLOW,
    }
    assert all(not item.blocker_codes for item in path.elements)
    assert path.limitations == ()
    assert list(path.evidence_ids) == sorted(path.evidence_ids)


# Each variant decides one element of one source's path, and so the composed path.
@pytest.mark.parametrize(
    ("variant", "source", "element", "decision"),
    [
        ({"function": {}}, "worker", E.DATABASE_NACL, D.ALLOW),
        ({"acl": [acl_entry(90, "deny"), *OPEN], "function": {}}, "worker", E.SOURCE_NACL, D.DENY),
        ({"rules": [ALLOW_ALL, DB_EGRESS]}, "i-plain", E.DATABASE_INGRESS, D.DENY),
        ({"rules": [REFERENCE, DB_EGRESS]}, "i-plain", E.SOURCE_EGRESS, D.DENY),
        (
            {"rules": [rule("sg-a", cidr="10.0.0.0/16"), ALLOW_ALL, DB_EGRESS], "function": {}},
            "worker",
            E.DATABASE_INGRESS,
            D.ALLOW,
        ),
        (
            {"rules": [rule("sg-a", cidr="10.0.2.10/32"), ALLOW_ALL, DB_EGRESS]},
            "i-multi",
            E.DATABASE_INGRESS,
            D.ALLOW,
        ),
        (
            {
                "rds": rds(subnets=("subnet-a", "subnet-b")),
                "acl": [acl_entry(90, "deny", cidr="10.0.1.0/24"), *OPEN],
            },
            "i-plain",
            E.DATABASE_NACL,
            D.DENY,
        ),
        ({"rds": rds(groups=[])}, "i-plain", E.DATABASE_INGRESS, D.UNKNOWN),
    ],
    ids=[
        "other-subnet-passes-both-acls",
        "source-acl-denies",
        "no-database-ingress",
        "no-source-egress",
        "range-covers-unaddressed-subnet",
        "second-interface-qualifies",
        "any-database-subnet-denies",
        "database-groups-unrecorded",
    ],
)
def test_each_variant_decides_its_element_and_the_composed_path(variant, source, element, decision):
    path = evaluate(**variant)[source]
    assert decisions(path)[element] == decision
    assert path.decision == decision


def test_incomplete_candidates_are_unknown_with_every_element_unknown():
    path = evaluate()["i-nosg"]
    assert path.decision == D.UNKNOWN
    assert list(decisions(path)) == list(AccessElement)
    assert set(decisions(path).values()) == {D.UNKNOWN}
    assert "source_security_groups_unrecorded" in {item.code for item in path.limitations}
