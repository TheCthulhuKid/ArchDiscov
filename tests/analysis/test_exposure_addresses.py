import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import ExposureElement, ExposureProtocol, NetworkDecision
from archdiscov.services.exposure_addresses import evaluate_public_entry, public_entries
from tests.support.exposure_data import PRIVATE, PUBLIC, build, eni, instance, sg_rule
from tests.support.nacl_data import entry as nacl_entry

D = NetworkDecision
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
    assert all(item.start != 22 for item in web)  # the rule for a private range


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            {
                "rules": [
                    sg_rule("sg-web", 80, to_port=90),
                    sg_rule("sg-web", 85, to_port=100),
                    sg_rule("sg-web", 101, to_port=110),
                    sg_rule("sg-web", 53, protocol="udp"),
                ]
            },
            [(TCP, 80, 110, D.ALLOW), (UDP, 53, 53, D.ALLOW)],
        ),
        (
            {"rules": [sg_rule("sg-web", -1, to_port=-1, protocol="-1")]},
            [(TCP, 1, 65535, D.ALLOW), (UDP, 1, 65535, D.ALLOW)],
        ),
        (
            {
                "acl": [
                    nacl_entry(100, ports=(85, 85)),
                    nacl_entry(100, egress=True, protocol="-1", ports=None),
                ]
            },
            [
                (TCP, 80, 84, D.DENY),
                (TCP, 85, 85, D.ALLOW),
                (TCP, 86, 90, D.DENY),
                (TCP, 443, 443, D.DENY),
            ],
        ),
    ],
    ids=["overlapping-ranges-merge", "all-traffic-opens-both-protocols", "acl-entries-split"],
)
def test_open_ranges_merge_per_protocol_and_split_where_acl_entries_apply(options, expected):
    _, found = evaluate(**options)
    assert spans(found["eni-web"]) == expected


# ---- blocked ingress ----------------------------------------------------------------


def test_a_public_address_with_blocked_ingress_is_a_cited_denial():
    _, found = evaluate()
    [closed] = found["eni-closed"]
    assert (closed.protocol, closed.start, closed.end) == (TCP, 1, 65535)
    assert closed.decision == D.DENY
    assert decisions(closed) == {X.PUBLIC_ADDRESS: D.ALLOW, X.SECURITY_GROUP: D.DENY}
    assert "denied" in codes(closed)
    assert closed.evidence_ids


# ---- routing evidence ---------------------------------------------------------------


def test_a_public_address_in_a_subnet_routed_through_nat_is_denied_never_exposed():
    enis = [eni("eni-nat", PRIVATE, ["sg-web"], public="198.51.100.40")]
    _, found = evaluate(enis=enis, instances=[{"Instances": []}])
    [only] = [e for e in found["eni-nat"] if e.start == 80]
    # A NAT gateway carries no inbound traffic, so the route is a cited denial.
    assert decisions(only)[X.ROUTE] == D.DENY
    assert only.decision == D.DENY


def test_missing_route_or_acl_records_are_unknown():
    _, found = evaluate(tables=False)
    assert decisions(found["eni-web"][0])[X.ROUTE] == D.UNKNOWN
    assert found["eni-web"][0].decision == D.UNKNOWN
    _, found = evaluate(acls=False)
    assert decisions(found["eni-web"][0])[X.NACL] == D.UNKNOWN


def test_limited_address_collection_keeps_a_candidate_with_an_unknown_address():
    _, found = evaluate(covered={"DescribeNetworkInterfaces": "failed"})
    # Only resources with a recorded address are entries: private ones are not turned into
    # unknown entries just because collection of their addresses was limited.
    assert set(found) == {"eni-web", "eni-closed", "i-public"}
    assert found["eni-web"][0].decision == D.UNKNOWN
    assert decisions(found["eni-web"][0])[X.PUBLIC_ADDRESS] == D.UNKNOWN


# ---- unsupported and unplaceable ----------------------------------------------------


def test_ipv6_rules_are_reported_and_never_an_exposure():
    rule = sg_rule("sg-closed", 5432, CidrIpv6="::/0", cidr=None)
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
    assert decisions(bare)[X.SECURITY_GROUP] == D.UNKNOWN
    assert "source_interface_incomplete" in codes(bare)
