import re

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.database_access import DatabaseAccess
from archdiscov.models.enums import (
    AccessElement,
    AccessSourceKind,
    AwsService,
    NetworkBlockerCode,
    NetworkDecision,
)
from archdiscov.services.database_access_paths import evaluate_candidate
from archdiscov.services.database_placement import resolve_database_placement
from archdiscov.services.database_sources import discover_sources
from tests.support.rds_data import INSTANCE, data
from tests.support.source_data import (
    acl_entry,
    build,
    ec2_items,
    rid,
    rule,
)

D = NetworkDecision
B = NetworkBlockerCode
E = AccessElement
ALLOW_ALL = rule("sg-app", egress=True, protocol="-1", cidr="0.0.0.0/0")
DB_EGRESS = rule("sg-a", egress=True, protocol="-1", cidr="0.0.0.0/0")
REFERENCE = rule("sg-a", referenced="sg-app")


def evaluate(port=5432, rules=None, task=None, function=None, rds=None, **network):
    items = ec2_items(rules=rules, **network)
    index = GraphIndex(build(ec2=items, task=task, function=function, rds=rds))
    placement = resolve_database_placement(index, rid(INSTANCE, AwsService.RDS), 5432)
    discovery = discover_sources(index, placement, 100)
    found = {
        re.split(r"[:/]", c.resource.identity.physical_id)[-1]: evaluate_candidate(
            index, placement, c, port
        )
        for c in discovery.candidates
    }
    return index, found


def elements(evaluation):
    return {item.element: item for item in evaluation.elements}


def decisions(evaluation):
    return {item.element: item.decision for item in evaluation.elements}


# ---- allowed paths ------------------------------------------------------------------


def test_an_instance_in_the_database_subnet_is_allowed_through_a_group_reference():
    _index, found = evaluate()
    path = found["i-plain"]
    assert path.decision == D.ALLOW
    assert decisions(path) == {
        E.SOURCE_EGRESS: D.ALLOW,
        E.DATABASE_INGRESS: D.ALLOW,
        E.ROUTE: D.ALLOW,
    }
    assert all(not item.blocker_codes for item in path.elements)
    assert path.limitations == ()
    assert list(path.evidence_ids) == sorted(path.evidence_ids)


def test_a_source_in_another_subnet_also_needs_both_network_acls():
    _, found = evaluate(function={})
    path = found["worker"]
    assert path.decision == D.ALLOW
    assert list(decisions(path)) == list(AccessElement)
    assert {item.element for item in path.elements if item.decision == D.ALLOW} == set(E)


def test_a_cidr_rule_matches_a_recorded_address_and_a_covering_range_without_one():
    cidr = rule("sg-a", cidr="10.0.0.0/16")
    _, found = evaluate(rules=[cidr, ALLOW_ALL, DB_EGRESS], function={})
    assert found["i-plain"].decision == D.ALLOW  # recorded address inside the range
    assert found["worker"].decision == D.ALLOW  # no address: its whole subnet is inside


def test_the_best_alternative_interface_is_reported():
    only_second = rule("sg-a", cidr="10.0.2.10/32")
    _, found = evaluate(rules=[only_second, ALLOW_ALL, DB_EGRESS])
    assert found["i-multi"].decision == D.ALLOW  # the subnet-b interface qualifies
    assert found["i-plain"].decision == D.DENY  # its address does not


# ---- denied paths -------------------------------------------------------------------


def test_a_database_without_a_matching_ingress_rule_denies_with_a_cited_blocker():
    _, found = evaluate(rules=[ALLOW_ALL, DB_EGRESS])
    path = found["i-plain"]
    assert path.decision == D.DENY
    assert decisions(path)[E.DATABASE_INGRESS] == D.DENY
    assert elements(path)[E.DATABASE_INGRESS].blocker_codes == [B.DENIED]
    assert any(item.code == "denied" and item.evidence_ids for item in path.limitations)


def test_a_source_without_egress_and_a_closed_port_both_deny():
    _, found = evaluate(rules=[REFERENCE, DB_EGRESS])
    assert decisions(found["i-plain"])[E.SOURCE_EGRESS] == D.DENY
    _, found = evaluate(port=3306)
    assert found["i-plain"].decision == D.DENY


def test_nacl_denial_and_a_missing_return_path_deny():
    deny_request = [acl_entry(90, "deny"), acl_entry(100), acl_entry(100, egress=True)]
    _, found = evaluate(acl=deny_request, function={})
    assert decisions(found["worker"])[E.SOURCE_NACL] == D.DENY
    assert found["i-plain"].decision == D.ALLOW  # same subnet: no ACL applies
    no_return = [acl_entry(100)]
    _, found = evaluate(acl=no_return, function={})
    assert found["worker"].decision == D.DENY
    assert D.DENY in {item.decision for item in found["worker"].elements}


def test_a_denial_dominates_an_unknown_element():
    _, found = evaluate(rules=[ALLOW_ALL, DB_EGRESS], acls=False, function={})
    path = found["worker"]
    assert decisions(path)[E.DATABASE_INGRESS] == D.DENY
    assert decisions(path)[E.SOURCE_NACL] == D.UNKNOWN
    assert path.decision == D.DENY


# ---- unknown paths ------------------------------------------------------------------


def test_missing_nacl_or_route_evidence_is_unknown_never_allowed():
    _, found = evaluate(acls=False, function={})
    path = found["worker"]
    assert path.decision == D.UNKNOWN
    assert elements(path)[E.SOURCE_NACL].blocker_codes == [B.MISSING_EVIDENCE]
    assert found["i-plain"].decision == D.ALLOW  # same subnet needs no ACL
    _, found = evaluate(tables=False)
    assert decisions(found["i-plain"])[E.ROUTE] == D.UNKNOWN
    assert found["i-plain"].decision == D.UNKNOWN


def test_unsupported_elements_stay_unknown():
    prefix = {**REFERENCE, "PrefixListId": "pl-1"}
    del prefix["ReferencedGroupInfo"]
    _, found = evaluate(rules=[prefix, ALLOW_ALL, DB_EGRESS])
    path = found["i-plain"]
    assert path.decision == D.UNKNOWN
    assert elements(path)[E.DATABASE_INGRESS].blocker_codes == [B.UNSUPPORTED_ELEMENT]


def test_a_range_that_covers_only_part_of_an_unknown_address_is_unknown():
    partial = rule("sg-a", cidr="10.0.2.0/25")
    _, found = evaluate(rules=[partial, ALLOW_ALL, DB_EGRESS], function={})
    assert found["worker"].decision == D.UNKNOWN
    assert elements(found["worker"])[E.DATABASE_INGRESS].blocker_codes == [B.MISSING_EVIDENCE]


def test_incomplete_candidates_are_unknown_with_every_element_unknown():
    _, found = evaluate()
    path = found["i-nosg"]
    assert path.decision == D.UNKNOWN
    assert list(decisions(path)) == list(AccessElement)
    assert set(decisions(path).values()) == {D.UNKNOWN}
    assert "source_security_groups_unrecorded" in {item.code for item in path.limitations}


def test_an_unrecorded_database_security_group_is_unknown_not_denied():
    value = data()
    value["describe_db_instances"]["DBInstances"][0]["VpcSecurityGroups"] = []
    _, found = evaluate(rds=value)
    path = found["i-plain"]
    assert decisions(path)[E.DATABASE_INGRESS] == D.UNKNOWN
    assert path.decision == D.UNKNOWN


def test_every_possible_database_subnet_must_work_not_just_one():
    value = data()
    group = value["describe_db_instances"]["DBInstances"][0]["DBSubnetGroup"]
    group["Subnets"] = [{"SubnetIdentifier": "subnet-a"}, {"SubnetIdentifier": "subnet-b"}]
    blocked = [
        acl_entry(90, "deny", cidr="10.0.1.0/24"),
        acl_entry(100),
        acl_entry(100, egress=True),
    ]
    _, found = evaluate(rds=value, acl=blocked)
    # Inside subnet-a nothing filters the traffic, but a database in subnet-b is denied.
    assert found["i-plain"].decision == D.DENY
    assert decisions(found["i-plain"])[E.DATABASE_NACL] == D.DENY
    _, open_found = evaluate(rds=value)
    assert open_found["i-plain"].decision == D.ALLOW


# ---- contract -----------------------------------------------------------------------


def test_every_evaluation_builds_a_valid_database_access_block():
    _, found = evaluate(function={}, task={})
    for path in found.values():
        access = DatabaseAccess(
            source_kind=AccessSourceKind.EC2_INSTANCE,
            port=5432,
            decision=path.decision,
            elements=list(path.elements),
        )
        assert access.decision == path.decision


def test_evaluation_is_deterministic():
    first = evaluate(function={}, task={})[1]
    second = evaluate(function={}, task={})[1]
    assert first == second
