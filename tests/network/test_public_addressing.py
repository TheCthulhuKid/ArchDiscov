import pytest

from archdiscov.graph import GraphIndex
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision
from archdiscov.services import load_snapshot
from archdiscov.services.network_addressing import evaluate_public_address
from tests.paths import FIXTURES_ROOT
from tests.support.network_data import ENI
from tests.support.network_snapshot import build_network, resource_id

D = NetworkDecision
B = NetworkBlockerCode
PRIVATE_ENI = {k: v for k, v in ENI.items() if k != "Association"}
INSTANCE = {"InstanceId": "i-1", "SubnetId": "subnet-1", "VpcId": "vpc-1"}
PUBLIC_INSTANCE = {**INSTANCE, "PublicIpAddress": "198.51.100.7"}
COVERED = {"DescribeNetworkInterfaces": "success", "DescribeInstances": "success"}


def check(target, *, enis=(), instances=(), covered=COVERED):
    items = {Op.DESCRIBE_NETWORK_INTERFACES: list(enis)}
    if instances:
        items[Op.DESCRIBE_INSTANCES] = [{"Instances": list(instances)}]
    index = GraphIndex(build_network(items, covered=covered))
    return index, evaluate_public_address(index, resource_id(target))


def codes(result):
    return {blocker.code for blocker in result.blockers}


def test_a_public_eni_allows_citing_the_address_fact():
    index, result = check("eni-1", enis=[ENI])
    eni = index.get_resource(resource_id("eni-1"))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(eni.fact_evidence["/public_ip"])


def test_a_private_eni_is_a_recorded_absence_citing_its_evidence():
    index, result = check("eni-1", enis=[PRIVATE_ENI])
    [blocker] = result.blockers
    assert result.decision == D.DENY
    assert (blocker.code, blocker.element) == (B.DENIED, "implicit_deny")
    assert set(index.resource_evidence_ids(resource_id("eni-1"))) <= set(blocker.evidence_ids)


def test_a_public_address_on_a_secondary_private_address_counts():
    secondary = {
        **PRIVATE_ENI,
        "PrivateIpAddresses": [
            {"PrivateIpAddress": "10.0.1.5", "Primary": True},
            {"PrivateIpAddress": "10.0.1.6", "Association": {"PublicIp": "203.0.113.9"}},
        ],
    }
    index, result = check("eni-1", enis=[secondary])
    eni = index.get_resource(resource_id("eni-1"))
    assert result.decision == D.ALLOW
    assert result.evidence_ids == sorted(eni.fact_evidence["/private_ip_addresses"])


def test_public_and_private_instances():
    index, public = check("i-1", instances=[PUBLIC_INSTANCE])
    instance = index.get_resource(resource_id("i-1"))
    assert public.decision == D.ALLOW
    assert public.evidence_ids == sorted(instance.fact_evidence["/attributes/public_ip_address"])
    assert check("i-1", instances=[INSTANCE])[1].decision == D.DENY


def test_an_ipv6_only_resource_is_a_recorded_absence_of_ipv4():
    ipv6 = {**INSTANCE, "PublicIpAddress": "2001:db8::1"}
    assert check("i-1", instances=[ipv6])[1].decision == D.DENY


def test_an_unparsable_address_is_unsupported():
    result = check("i-1", instances=[{**INSTANCE, "PublicIpAddress": "not-an-ip"}])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.UNSUPPORTED_ELEMENT}


def test_addresses_omitted_during_normalization_make_absence_unknown():
    omitted = {**PRIVATE_ENI, "PrivateIpAddresses": [{"PrivateIpAddress": ""}]}
    result = check("eni-1", enis=[omitted])[1]
    assert result.decision == D.UNKNOWN
    assert codes(result) == {B.MISSING_EVIDENCE}


def test_unrecorded_resources_and_other_types_are_unknown():
    index = check("eni-1", enis=[ENI])[0]
    for target in ("eni-9", "subnet-1"):
        result = evaluate_public_address(index, resource_id(target))
        assert result.decision == D.UNKNOWN
        assert codes(result) == {B.MISSING_EVIDENCE}


@pytest.mark.parametrize("status", ["failed", "incomplete", "skipped"])
def test_limited_collection_of_the_resources_operation_gives_unknown(status):
    for target, kwargs, operation in (
        ("eni-1", {"enis": [ENI]}, "DescribeNetworkInterfaces"),
        ("eni-1", {"enis": [PRIVATE_ENI]}, "DescribeNetworkInterfaces"),
        ("i-1", {"instances": [PUBLIC_INSTANCE]}, "DescribeInstances"),
    ):
        result = check(target, covered={**COVERED, operation: status}, **kwargs)[1]
        assert result.decision == D.UNKNOWN
        assert B.COVERAGE_LIMITED in codes(result)


def test_other_operations_do_not_limit_the_decision():
    covered = {**COVERED, "DescribeInstances": "failed"}
    assert check("eni-1", enis=[ENI], covered=covered)[1].decision == D.ALLOW


def test_non_aws_snapshots_return_the_unsupported_platform_result():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "azure.json"))
    result = evaluate_public_address(index, "any")
    assert codes(result) == {B.UNSUPPORTED_PLATFORM}
