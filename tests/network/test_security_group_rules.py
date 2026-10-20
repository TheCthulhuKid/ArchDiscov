import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision, RuleDirection
from archdiscov.services import load_snapshot
from archdiscov.services.network_security_groups import Peer, evaluate_security_group
from tests.paths import FIXTURES_ROOT
from tests.support.sg_rule_data import build, group_id, rule

D = NetworkDecision
B = NetworkBlockerCode
INGRESS, EGRESS = RuleDirection.INGRESS, RuleDirection.EGRESS
LOCAL = "10.0.1.5"


def check(groups, *, direction=INGRESS, peer=None, protocol="tcp", port=443, group="sg-app", **kw):
    snapshot = build(groups, **kw)
    index = GraphIndex(snapshot)
    return index, evaluate_security_group(
        index,
        group_id(group),
        direction,
        peer or Peer.address(LOCAL, ()),
        protocol=protocol,
        port=port,
    )


def decision(groups, **kwargs):
    return check(groups, **kwargs)[1].decision


def codes(result):
    return {blocker.code for blocker in result.blockers}


# ---- CIDR ---------------------------------------------------------------------------


def test_cidr_allow_cites_the_matching_rule_and_outside_addresses_are_denied():
    index, result = check({"sg-app": [rule(cidr="10.0.0.0/16")]})
    group = index.get_resource(group_id("sg-app"))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(group.fact_evidence["/ingress_rules/0"])
    _, outside = check({"sg-app": [rule(cidr="10.0.0.0/16")]}, peer=Peer.address("192.168.1.1", ()))
    assert outside.decision == D.DENY
    [blocker] = outside.blockers
    assert (blocker.code, blocker.element) == (B.DENIED, "implicit_deny")
    assert blocker.evidence_ids == sorted(index.resource_evidence_ids(group_id("sg-app")))


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        (Peer.cidr("10.0.5.0/24", ()), D.ALLOW),
        (Peer.cidr("10.0.0.0/16", ()), D.ALLOW),
        (Peer.cidr("10.0.0.0/8", ()), D.DENY),
        (Peer.cidr("0.0.0.0/0", ()), D.DENY),
    ],
)
def test_peer_networks_must_be_contained_by_the_rule_cidr(peer, expected):
    assert decision({"sg-app": [rule(cidr="10.0.0.0/16")]}, peer=peer) == expected


def test_open_cidr_allows_every_ipv4_peer_and_an_unknown_peer_address_is_unknown():
    assert (
        decision({"sg-app": [rule(cidr="0.0.0.0/0")]}, peer=Peer.cidr("0.0.0.0/0", ())) == D.ALLOW
    )
    result = check({"sg-app": [rule(cidr="10.0.0.0/16")]}, peer=Peer(None, ()))[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


# ---- referenced groups --------------------------------------------------------------


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ((group_id("sg-web"),), D.ALLOW),
        ((group_id("sg-db"),), D.DENY),
        ((), D.DENY),
        ((group_id("sg-db"), group_id("sg-web")), D.ALLOW),
        (None, D.UNKNOWN),
    ],
)
def test_group_references_match_the_peers_group_membership(members, expected):
    groups = {
        "sg-app": [rule(referenced_group_id="sg-web")],
        "sg-web": [],
        "sg-db": [],
    }
    peer = Peer(None, members)
    assert decision(groups, peer=peer) == expected


def test_unrecorded_peer_groups_are_unknown():
    result = check({"sg-app": [rule(referenced_group_id="sg-web")]}, peer=Peer(None, ("absent",)))[
        1
    ]
    assert result.decision == D.UNKNOWN


# ---- protocol and ports -------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "query", "port", "expected"),
    [
        ("-1", "tcp", 1, D.ALLOW),
        ("-1", "icmp", 8, D.ALLOW),
        ("tcp", "tcp", 443, D.ALLOW),
        ("6", "tcp", 443, D.ALLOW),
        ("TCP", "6", 443, D.ALLOW),
        ("tcp", "udp", 443, D.DENY),
        ("udp", "17", 443, D.ALLOW),
        ("icmp", "icmp", 443, D.ALLOW),
        ("1", "icmp", 443, D.ALLOW),
        ("icmp", "tcp", 443, D.DENY),
    ],
)
def test_protocols_match_by_name_or_number_and_all_protocol_ignores_ports(
    protocol, query, port, expected
):
    groups = {"sg-app": [rule(protocol=protocol, cidr="10.0.0.0/16")]}
    assert decision(groups, protocol=query, port=port) == expected


@pytest.mark.parametrize(
    ("port", "expected"),
    [(999, D.DENY), (1000, D.ALLOW), (1500, D.ALLOW), (2000, D.ALLOW), (2001, D.DENY)],
)
def test_port_ranges_are_inclusive(port, expected):
    groups = {"sg-app": [rule(from_port=1000, to_port=2000, cidr="10.0.0.0/16")]}
    assert decision(groups, port=port) == expected


def test_all_ports_and_unrecorded_ranges_or_protocols():
    assert (
        decision({"sg-app": [rule(from_port=-1, to_port=-1, cidr="10.0.0.0/16")]}, port=9)
        == D.ALLOW
    )
    no_range = rule(from_port=None, to_port=None, cidr="10.0.0.0/16")
    result = check({"sg-app": [no_range]})[1]
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.MISSING_EVIDENCE})
    no_protocol = rule(cidr="10.0.0.0/16").model_copy(update={"protocol": None})
    result = check({"sg-app": [no_protocol]})[1]
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.MISSING_EVIDENCE})


# ---- direction ----------------------------------------------------------------------


def test_ingress_and_egress_rules_are_evaluated_independently():
    groups = {"sg-app": [rule("ingress", cidr="10.0.0.0/16")]}
    assert decision(groups, direction=INGRESS) == D.ALLOW
    assert decision(groups, direction=EGRESS) == D.DENY
    groups = {"sg-app": [rule("egress", cidr="10.0.0.0/16")]}
    assert decision(groups, direction=EGRESS) == D.ALLOW
    assert decision(groups, direction=INGRESS) == D.DENY


# ---- unsupported --------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsupported_rule",
    [
        rule(cidr="2001:db8::/32"),
        rule(prefix_list_id="pl-123"),
        rule(referenced_group_id="sg-partner", referenced_account_id="210987654321"),
        rule(protocol="50", from_port=None, to_port=None, cidr="10.0.0.0/16"),
    ],
    ids=["ipv6", "prefix-list", "cross-account-group", "unrecognized-protocol"],
)
def test_unsupported_rules_never_allow_but_cannot_be_ruled_out(unsupported_rule):
    result = check({"sg-app": [unsupported_rule]}, peer=Peer.address(LOCAL, ()))[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}
    # A supported rule that allows still wins over an unsupported alternative.
    allowed = check({"sg-app": [unsupported_rule, rule(cidr="10.0.0.0/16")]})[1]
    assert allowed.decision == D.ALLOW


def test_unsupported_query_protocols_are_unknown():
    result = check({"sg-app": [rule(protocol="-1", cidr="0.0.0.0/0")]}, protocol="gre")[1]
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.UNSUPPORTED_ELEMENT})


def test_ipv6_peers_cannot_be_constructed():
    with pytest.raises(ValueError):
        Peer.cidr("2001:db8::/32")


# ---- missing evidence ---------------------------------------------------------------


def test_unrecorded_groups_and_non_groups_are_unknown():
    index = GraphIndex(build({"sg-app": []}))
    for target in ("absent", group_id("sg-other")):
        result = evaluate_security_group(
            index, target, INGRESS, Peer.address(LOCAL, ()), protocol="tcp", port=1
        )
        assert (result.decision, codes(result)) == (D.UNKNOWN, {B.MISSING_EVIDENCE})


def test_empty_rule_lists_deny_only_when_the_group_has_evidence():
    assert decision({"sg-app": []}) == D.DENY
    result = check({"sg-app": []}, bare=("sg-app",))[1]
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.MISSING_EVIDENCE})


def test_a_matching_rule_without_a_citation_cannot_allow():
    result = check({"sg-app": [rule(cidr="10.0.0.0/16")]}, uncited=True)[1]
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.MISSING_EVIDENCE})


# ---- collection ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped", "unsupported"])
@pytest.mark.parametrize("allowed", [True, False])
def test_limited_rule_collection_makes_every_decision_unknown(status, allowed):
    cidr = "10.0.0.0/16" if allowed else "172.16.0.0/16"
    result = check({"sg-app": [rule(cidr=cidr)]}, status=status)[1]
    assert result.decision == D.UNKNOWN
    [limited] = [b for b in result.blockers if b.code == B.COVERAGE_LIMITED]
    assert limited.coverage.operation == "DescribeSecurityGroups"


def test_successful_collection_does_not_limit_and_absent_records_are_not_interpreted():
    assert decision({"sg-app": [rule(cidr="10.0.0.0/16")]}, status="success") == D.ALLOW
    assert decision({"sg-app": [rule(cidr="10.0.0.0/16")]}, status=None) == D.ALLOW


# ---- platform and determinism -------------------------------------------------------


def test_non_aws_snapshots_return_the_unsupported_platform_result():
    azure = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    result = evaluate_security_group(
        azure, azure.resource_ids[0], INGRESS, Peer.address(LOCAL, ()), protocol="tcp", port=1
    )
    assert (result.decision, codes(result)) == (D.UNKNOWN, {B.UNSUPPORTED_PLATFORM})


def test_results_are_deterministic_and_detached():
    groups = {"sg-app": [rule(cidr="10.0.0.0/16"), rule(cidr="10.0.0.0/8")]}
    first = check(groups)[1]
    assert first == check(groups)[1]
    first.evidence_ids.append("changed")
    assert "changed" not in check(groups)[1].evidence_ids


def test_rules_omitted_during_normalization_make_an_implicit_deny_unknown():
    omitted = ("sg-app",)
    result = check({"sg-app": [rule(cidr="192.168.0.0/16")]}, omitted=omitted)[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}
    allowed = check({"sg-app": [rule(cidr="10.0.0.0/16")]}, omitted=omitted)[1]
    assert allowed.decision == D.ALLOW
