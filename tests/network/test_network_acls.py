import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RuleDirection
from archdiscov.services import load_snapshot
from archdiscov.services.network_acls import evaluate_network_acl
from tests.paths import FIXTURES_ROOT
from tests.support.nacl_data import ALL, acl, build, entry
from tests.support.network_snapshot import resource_id

D = NetworkDecision
B = NetworkBlockerCode
INGRESS, EGRESS = RuleDirection.INGRESS, RuleDirection.EGRESS


def check(entries, *, direction=INGRESS, address="10.0.1.5", protocol="tcp", port=443, **kw):
    acls = kw.pop("acls", None) or [acl("acl-1", entries, "subnet-1")]
    index = GraphIndex(build(acls, **kw))
    return index, evaluate_network_acl(
        index, resource_id("subnet-1"), direction, address, protocol=protocol, port=port
    )


def decision(entries, **kwargs):
    return check(entries, **kwargs)[1].decision


def codes(result):
    return {blocker.code for blocker in result.blockers}


# ---- ordering -----------------------------------------------------------------------


def test_lowest_rule_number_wins_regardless_of_recorded_order():
    allow_first = [entry(200, "deny"), entry(100, "allow")]
    deny_first = [entry(200, "allow"), entry(100, "deny")]
    assert decision(allow_first) == D.ALLOW
    assert decision(deny_first) == D.DENY


def test_allow_before_deny_cites_the_matching_entry_and_association():
    index, result = check([entry(100, "allow"), entry(110, "deny")])
    acl_resource = index.get_resource(resource_id("acl-1"))
    [edge] = index.incoming_edges(resource_id("subnet-1"))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(
        [*acl_resource.fact_evidence["/entries/0"], *edge.evidence_ids]
    )


def test_deny_before_allow_is_a_cited_denied_blocker():
    index, result = check([entry(100, "deny"), entry(110, "allow")])
    acl_resource = index.get_resource(resource_id("acl-1"))
    [blocker] = result.blockers
    assert result.decision == D.DENY
    assert (blocker.code, blocker.element) == (B.DENIED, "network_acl_entry:/entries/0")
    assert set(acl_resource.fact_evidence["/entries/0"]) <= set(blocker.evidence_ids)


def test_non_matching_entries_are_skipped():
    entries = [
        entry(100, "deny", ports=(80, 80)),
        entry(110, "deny", protocol="17"),
        entry(120, "deny", CidrBlock="192.168.0.0/16"),
        entry(130, "deny", egress=True),
        entry(140, "allow"),
    ]
    assert decision(entries) == D.ALLOW


def test_the_implicit_final_deny_cites_the_acl_and_association():
    index, result = check([entry(100, "allow", ports=(80, 80))])
    [blocker] = result.blockers
    [edge] = index.incoming_edges(resource_id("subnet-1"))
    assert result.decision == D.DENY
    assert (blocker.code, blocker.element) == (B.DENIED, "implicit_deny")
    assert set(index.resource_evidence_ids(resource_id("acl-1"))) <= set(blocker.evidence_ids)
    assert set(edge.evidence_ids) <= set(blocker.evidence_ids)


def test_the_recorded_default_entry_denies_and_an_empty_acl_is_an_implicit_deny():
    assert decision([ALL]) == D.DENY
    assert decision([]) == D.DENY


# ---- direction ----------------------------------------------------------------------


def test_each_direction_is_evaluated_independently():
    entries = [entry(100, "allow"), entry(100, "deny", egress=True)]
    assert decision(entries, direction=INGRESS) == D.ALLOW
    assert decision(entries, direction=EGRESS) == D.DENY
    assert decision([entry(100, "allow", egress=True)], direction=INGRESS) == D.DENY


# ---- matching -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [("10.0.1.5", D.ALLOW), ("10.0.255.255", D.ALLOW), ("10.1.0.1", D.DENY)],
)
def test_the_address_must_be_inside_the_entry_cidr(address, expected):
    assert decision([entry(100, CidrBlock="10.0.0.0/16")], address=address) == expected


@pytest.mark.parametrize(
    ("protocol", "port", "expected"),
    [("tcp", 443, D.ALLOW), ("6", 443, D.ALLOW), ("tcp", 444, D.DENY), ("udp", 443, D.DENY)],
)
def test_protocol_and_port_range(protocol, port, expected):
    assert decision([entry(100)], protocol=protocol, port=port) == expected


def test_all_protocols_and_port_range_bounds_are_inclusive():
    assert decision([entry(100, protocol="-1", ports=None)], protocol="udp", port=9) == D.ALLOW
    ranges = [entry(100, ports=(1024, 65535))]
    assert decision(ranges, port=1024) == D.ALLOW
    assert decision(ranges, port=65535) == D.ALLOW
    assert decision(ranges, port=1023) == D.DENY


# ---- association --------------------------------------------------------------------


def test_a_subnet_without_an_observed_association_is_unknown_not_a_default_acl():
    result = check([], acls=[acl("acl-1", [entry(100)])])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}
    assert result.blockers[0].element == "network_acl_association"


def test_several_associations_are_ambiguous():
    acls = [acl("acl-1", [entry(100)], "subnet-1"), acl("acl-2", [entry(100)], "subnet-1")]
    result = check([], acls=acls)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_the_association_selects_the_subnets_acl():
    acls = [
        acl("acl-1", [entry(100, "allow")], "subnet-1"),
        acl("acl-2", [entry(100, "deny")], "subnet-2"),
    ]
    index = GraphIndex(build(acls, subnets=("subnet-1", "subnet-2")))
    results = {
        s: evaluate_network_acl(
            index, resource_id(s), INGRESS, "10.0.1.5", protocol="tcp", port=443
        ).decision
        for s in ("subnet-1", "subnet-2")
    }
    assert results == {"subnet-1": D.ALLOW, "subnet-2": D.DENY}


def test_an_unrecorded_subnet_is_unknown():
    index, _ = check([entry(100)])
    result = evaluate_network_acl(
        index, resource_id("subnet-9"), INGRESS, "10.0.1.5", protocol="tcp", port=443
    )
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


# ---- unsupported and missing entry data ---------------------------------------------


def test_an_unsupported_entry_ahead_of_the_match_makes_the_result_unknown():
    ipv6 = entry(100, "deny", CidrBlock=None, Ipv6CidrBlock="::/0")
    result = check([ipv6, entry(110, "allow")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}
    assert decision([entry(100, "allow"), ipv6]) == D.ALLOW


def test_an_unrecognized_protocol_entry_is_unsupported():
    result = check([entry(100, "deny", protocol="47"), entry(110, "allow")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_an_entry_without_a_port_range_that_could_match_is_unknown():
    result = check([entry(100, "allow", ports=None)])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}
    assert decision([entry(100, "allow", ports=None)], protocol="udp") == D.DENY


def test_entries_omitted_during_normalization_make_the_order_incomplete():
    result = check([entry(100, "allow"), entry(110, "unknown-action")])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_an_unsupported_query_protocol_is_unknown():
    result = check([entry(100)], protocol="gre")[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


# ---- collection coverage ------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
def test_limited_collection_downgrades_every_decision(status):
    for entries in ([entry(100, "allow")], [entry(100, "deny")], []):
        result = check(entries, status=status)[1]
        assert result.decision == D.UNKNOWN
        assert B.COVERAGE_LIMITED in codes(result)


def test_unrecorded_coverage_is_not_interpreted():
    assert decision([entry(100)], status=None) == D.ALLOW


# ---- platform -----------------------------------------------------------------------


def test_non_aws_snapshots_return_the_unsupported_platform_result():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    result = evaluate_network_acl(index, "any", INGRESS, "10.0.1.5", protocol="tcp", port=443)
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_PLATFORM}
