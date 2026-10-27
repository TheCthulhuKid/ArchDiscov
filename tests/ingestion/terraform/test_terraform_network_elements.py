"""Phase 6 network elements imported from Terraform state and plans."""

from archdiscov.ingestion.terraform import TerraformStateRequest
from archdiscov.models.resources.network import EgressOnlyInternetGateway, NatGateway
from archdiscov.services import collect_snapshots
from tests.paths import FIXTURES_ROOT
from tests.support.network_data import flatten
from tests.support.terraform_data import CONTEXT, collect

FIXTURES = FIXTURES_ROOT / "terraform"


def state(name):
    request = TerraformStateRequest(
        path=FIXTURES / f"{name}.state.json", account_id="123456789012", region="eu-west-1"
    )
    return collect_snapshots(request, context=CONTEXT)[0]


def by_key(snapshot, key):
    return {key(r): r for tree in snapshot.regions.values() for r in flatten(tree.resources)}


def references(snapshot):
    """(source, target, field) for each configuration reference, by physical ID or address."""
    names = {
        r.id: r.identity.physical_id or r.identity.logical_address
        for r in by_key(snapshot, lambda r: r.id).values()
    }
    return {
        (names[d.source_id], names.get(d.target.resource_id), d.attributes["field"])
        for deps in snapshot.dependencies_by_region.values()
        for d in deps
        if d.target.kind == "local"
    }


def test_state_maps_nat_and_egress_only_gateways_with_their_references():
    snapshot = state("egress-gateways")
    resources = by_key(snapshot, lambda r: r.identity.physical_id)
    nat, egress_only = resources["nat-public"], resources["eigw-main"]
    assert isinstance(nat, NatGateway) and isinstance(egress_only, EgressOnlyInternetGateway)
    assert nat in resources["subnet-public"].children
    assert egress_only in resources["vpc-egress"].children
    assert (nat.connectivity, nat.network_interface_id) == ("public", "eni-nat")
    assert [str(ip) for ip in nat.public_ips] == ["198.51.100.20"]
    assert nat.state is None  # Terraform declares no state, so none is assumed
    cited = snapshot.evidence[nat.fact_evidence["/public_ips/0"][0]]
    assert cited.configuration_path.endswith("/values/public_ip")
    assert {
        ("nat-public", "subnet-public", "subnet_id"),
        ("eigw-main", "vpc-egress", "vpc_id"),
    } <= references(snapshot)


def test_plan_keeps_unknown_nat_gateway_values_indeterminate():
    snapshot = collect(FIXTURES / "egress-gateways.plan.json")
    nat = by_key(snapshot, lambda r: r.identity.logical_address)["aws_nat_gateway.public"]
    assert isinstance(nat, NatGateway) and nat.connectivity == "public"
    assert nat.identity.physical_id is None and nat.network_interface_id is None
    assert "/network_interface_id" not in nat.fact_evidence and nat.public_ips == []
    unknown = {
        item.configuration_path.rsplit("/", 1)[-1]
        for item in snapshot.evidence.values()
        if item.value_status == "unresolved" and item.resource_id == nat.id
    }
    assert {"id", "network_interface_id", "public_ip", "subnet_id"} <= unknown
    assert (
        "aws_nat_gateway.public",
        "aws_subnet.public",
        "configuration_expression",
    ) in references(snapshot)
