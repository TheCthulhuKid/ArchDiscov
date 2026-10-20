import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import (
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    NetworkBlockerCode,
    NetworkDecision,
)
from archdiscov.models.resources.network import NetworkInterface
from archdiscov.services.exposure_addresses import evaluate_public_entry, public_entries
from archdiscov.services.exposure_ports import (
    PortRange,
    boundaries,
    internet_open_ports,
    split,
)
from tests.support.exposure_data import (
    PRIVATE,
    PUBLIC,
    build,
    eni,
    instance,
    sg_rule,
)

D = NetworkDecision
B = NetworkBlockerCode
X = ExposureElement
TCP, UDP = ExposureProtocol.TCP, ExposureProtocol.UDP


def evaluate(**options):
    index = GraphIndex(build(**options))
    found = {}
    for entry in public_entries(index):
        name = entry.identity.physical_id
        found[name] = evaluate_public_entry(index, entry)
    return index, found


def spans(evaluations):
    return [(e.protocol, e.start, e.end, e.decision) for e in evaluations]


def decisions(evaluation):
    return {item.element: item.decision for item in evaluation.elements}


def codes(evaluation):
    return [item.code for item in evaluation.limitations]


# ---- entry points -------------------------------------------------------------------


def test_only_resources_with_a_recorded_public_address_are_entries():
    _, found = evaluate()
    assert set(found) == {"eni-web", "eni-closed", "i-public"}  # private ones are absent


def test_an_instance_with_stored_interfaces_is_represented_by_them():
    enis = [eni("eni-1", PUBLIC, ["sg-web"], public="198.51.100.30", instance="i-attached")]
    instances = [
        {"Instances": [instance("i-attached", PUBLIC, ["sg-web"], public="198.51.100.30")]}
    ]
    _, found = evaluate(enis=enis, instances=instances)
    assert set(found) == {"eni-1"}


def test_entries_are_ordered_by_identity():
    index, found = evaluate()
    assert [e.id for e in public_entries(index)] == sorted(e.id for e in public_entries(index))
    assert all(e.kind == ExposureEntryKind.PUBLIC_ADDRESS for v in found.values() for e in v)


# ---- open ports ---------------------------------------------------------------------


def test_open_ranges_come_from_rules_open_to_the_whole_internet_and_cite_them():
    index, found = evaluate()
    web = found["eni-web"]
    assert spans(web) == [(TCP, 80, 90, D.ALLOW), (TCP, 443, 443, D.ALLOW)]
    group = next(r for r in index.resources() if r.identity.physical_id == "sg-web")
    cited = {
        e
        for pointer in group.fact_evidence
        if "ingress_rules" in pointer
        for e in group.fact_evidence[pointer]
    }
    assert cited and cited & set(web[1].evidence_ids)
    elements = {X.PUBLIC_ADDRESS, X.ROUTE, X.NACL, X.SECURITY_GROUP}
    assert all(set(decisions(item)) == elements for item in web)


def test_a_rule_for_a_private_range_is_not_an_exposure():
    _, found = evaluate()
    assert all(item.start != 22 for item in found["eni-web"])


def test_overlapping_and_adjacent_ranges_merge_per_protocol_deterministically():
    rules = [
        sg_rule("sg-web", 80, to_port=90),
        sg_rule("sg-web", 85, to_port=100),
        sg_rule("sg-web", 101, to_port=110),
        sg_rule("sg-web", 53, protocol="udp"),
    ]
    _, found = evaluate(rules=rules)
    assert spans(found["eni-web"]) == [
        (TCP, 80, 110, D.ALLOW),
        (UDP, 53, 53, D.ALLOW),
    ]


def test_all_protocols_and_all_ports_open_both_protocols_entirely():
    rules = [sg_rule("sg-web", -1, to_port=-1, protocol="-1")]
    _, found = evaluate(rules=rules)
    assert spans(found["eni-web"]) == [
        (TCP, 1, 65535, D.ALLOW),
        (UDP, 1, 65535, D.ALLOW),
    ]


def test_port_ranges_are_split_where_acl_entries_start_and_stop():
    acl = [
        {
            "RuleNumber": 100,
            "Protocol": "6",
            "RuleAction": "allow",
            "Egress": False,
            "CidrBlock": "0.0.0.0/0",
            "PortRange": {"From": 85, "To": 85},
        },
        {
            "RuleNumber": 100,
            "Protocol": "-1",
            "RuleAction": "allow",
            "Egress": True,
            "CidrBlock": "0.0.0.0/0",
        },
    ]
    _, found = evaluate(acl=acl)
    assert spans(found["eni-web"]) == [
        (TCP, 80, 84, D.DENY),
        (TCP, 85, 85, D.ALLOW),
        (TCP, 86, 90, D.DENY),
        (TCP, 443, 443, D.DENY),
    ]


# ---- blocked ingress ----------------------------------------------------------------


def test_a_public_address_with_blocked_ingress_is_a_cited_denial():
    _, found = evaluate()
    [closed] = found["eni-closed"]
    assert (closed.protocol, closed.start, closed.end) == (TCP, 1, 65535)
    assert closed.decision == D.DENY
    assert decisions(closed) == {X.PUBLIC_ADDRESS: D.ALLOW, X.SECURITY_GROUP: D.DENY}
    assert "denied" in codes(closed)
    assert closed.evidence_ids


def test_a_nacl_denial_blocks_the_ingress_and_a_missing_return_path_too():
    deny = [
        {
            "RuleNumber": 90,
            "Protocol": "-1",
            "RuleAction": "deny",
            "Egress": False,
            "CidrBlock": "0.0.0.0/0",
        },
        {
            "RuleNumber": 100,
            "Protocol": "-1",
            "RuleAction": "allow",
            "Egress": True,
            "CidrBlock": "0.0.0.0/0",
        },
    ]
    _, found = evaluate(acl=deny)
    assert {item.decision for item in found["eni-web"]} == {D.DENY}
    assert decisions(found["eni-web"][0])[X.NACL] == D.DENY
    request_only = [deny[1].copy() | {"Egress": False}]
    _, found = evaluate(acl=request_only)
    assert decisions(found["eni-web"][0])[X.NACL] == D.DENY  # no return path


def test_an_acl_entry_covering_only_part_of_the_internet_is_unknown_not_a_denial():
    partial = [
        {
            "RuleNumber": 90,
            "Protocol": "-1",
            "RuleAction": "deny",
            "Egress": False,
            "CidrBlock": "198.51.100.0/24",
        },
        {
            "RuleNumber": 100,
            "Protocol": "-1",
            "RuleAction": "allow",
            "Egress": False,
            "CidrBlock": "0.0.0.0/0",
        },
        {
            "RuleNumber": 100,
            "Protocol": "-1",
            "RuleAction": "allow",
            "Egress": True,
            "CidrBlock": "0.0.0.0/0",
        },
    ]
    _, found = evaluate(acl=partial)
    web = found["eni-web"][0]
    assert decisions(web)[X.NACL] == D.UNKNOWN
    assert web.decision == D.UNKNOWN


# ---- routing evidence ---------------------------------------------------------------


def test_a_public_address_in_a_subnet_routed_through_nat_is_unknown_never_exposed():
    enis = [eni("eni-nat", PRIVATE, ["sg-web"], public="198.51.100.40")]
    _, found = evaluate(enis=enis, instances=[{"Instances": []}])
    [only] = [e for e in found["eni-nat"] if e.start == 80]
    assert decisions(only)[X.ROUTE] == D.UNKNOWN
    assert only.decision == D.UNKNOWN
    assert "unsupported_element" in codes(only)


def test_missing_route_or_acl_records_are_unknown():
    _, found = evaluate(tables=False)
    assert decisions(found["eni-web"][0])[X.ROUTE] == D.UNKNOWN
    assert found["eni-web"][0].decision == D.UNKNOWN
    _, found = evaluate(acls=False)
    assert decisions(found["eni-web"][0])[X.NACL] == D.UNKNOWN


@pytest.mark.parametrize("operation", ["DescribeRouteTables", "DescribeNetworkAcls"])
@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
def test_limited_routing_or_acl_collection_makes_the_decision_unknown(operation, status):
    _, found = evaluate(covered={operation: status})
    web = found["eni-web"][0]
    assert web.decision == D.UNKNOWN
    assert "coverage_limited" in codes(web)


def test_limited_address_collection_keeps_a_candidate_with_an_unknown_address():
    _, found = evaluate(covered={"DescribeNetworkInterfaces": "failed"})
    # Only resources with a recorded address are entries: private ones are not turned into
    # unknown entries just because collection of their addresses was limited.
    assert set(found) == {"eni-web", "eni-closed", "i-public"}
    assert found["eni-web"][0].decision == D.UNKNOWN
    assert decisions(found["eni-web"][0])[X.PUBLIC_ADDRESS] == D.UNKNOWN


# ---- unsupported and unplaceable ----------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"PrefixListId": "pl-1", "cidr": None},
        {"CidrIpv6": "::/0", "cidr": None},
    ],
    ids=["prefix-list", "ipv6"],
)
def test_prefix_list_and_ipv6_rules_are_reported_and_never_an_exposure(extra):
    rule = sg_rule("sg-closed", 5432, **extra)
    _, found = evaluate(rules=[rule])
    [closed] = found["eni-closed"]
    assert closed.decision == D.UNKNOWN
    assert "exposure_rule_unsupported" in codes(closed)
    assert "unsupported_element" in codes(closed)


def test_group_references_are_reported_but_do_not_make_the_denial_uncertain():
    rule = sg_rule("sg-closed", 5432, cidr=None, ReferencedGroupInfo={"GroupId": "sg-web"})
    _, found = evaluate(rules=[rule])
    [closed] = found["eni-closed"]
    assert closed.decision == D.DENY
    assert "exposure_rule_unsupported" in codes(closed)


def test_an_entry_without_placement_is_unknown_on_the_whole_range():
    enis = [eni("eni-bare", PUBLIC, [], public="198.51.100.50")]
    _, found = evaluate(enis=enis, instances=[{"Instances": []}])
    [bare] = found["eni-bare"]
    assert (bare.start, bare.end, bare.decision) == (1, 65535, D.UNKNOWN)
    assert set(decisions(bare).values()) <= {D.ALLOW, D.UNKNOWN}
    assert "source_interface_incomplete" in codes(
        bare
    ) or "source_security_groups_unrecorded" in codes(bare)


def test_evaluation_is_deterministic():
    def summary(found):
        return {
            name: [(e.start, e.end, e.decision, e.evidence_ids) for e in evaluations]
            for name, evaluations in found.items()
        }

    assert summary(evaluate()[1]) == summary(evaluate()[1])


# ---- port range helpers -------------------------------------------------------------


def test_split_cuts_only_inside_the_range():
    piece = PortRange(TCP, 80, 90)
    assert split(piece, {1, 80, 85, 91, 500}) == [
        PortRange(TCP, 80, 84),
        PortRange(TCP, 85, 90),
    ]
    assert split(piece, set()) == [piece]


def test_boundaries_come_from_matching_rules_and_ingress_entries_only():
    index = GraphIndex(build())
    group = next(r for r in index.resources() if r.identity.physical_id == "sg-web")
    points = boundaries([group], [], TCP)
    assert {80, 91, 443, 444, 22, 23} <= points
    assert boundaries([group], [], UDP) == set()
    assert internet_open_ports([group]).ranges == (
        PortRange(TCP, 80, 90),
        PortRange(TCP, 443, 443),
    )


def test_resources_are_normalized_network_interfaces():
    index, _ = evaluate()
    assert any(isinstance(r, NetworkInterface) for r in index.resources())
