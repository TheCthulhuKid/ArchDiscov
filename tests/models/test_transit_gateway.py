"""Transit gateways, their attachments, and route tables as recorded facts."""

import json

import pytest
from pydantic import ValidationError

from archdiscov.models.enums import TransitGatewayRouteType
from archdiscov.models.resources.network import (
    TransitGateway,
    TransitGatewayAttachment,
    TransitGatewayRoute,
    TransitGatewayRouteTable,
)
from archdiscov.services import load_snapshot, save_snapshot
from archdiscov.services.snapshots import SnapshotValidationError, validate_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "transit-gateway.json"


def test_fixture_loads_typed_round_trips_and_cites_exact_entries(tmp_path):
    snapshot = load_snapshot(FIXTURE)
    gateway, app, data, table = snapshot.regions["eu-west-1"].resources[2:]
    assert isinstance(gateway, TransitGateway)
    assert gateway.association_default_route_table_id == table.identity.physical_id
    assert isinstance(app, TransitGatewayAttachment)
    assert isinstance(data, TransitGatewayAttachment)
    assert app.associated_route_table_id == "tgw-rtb-main"
    assert data.associated_route_table_id is None
    assert isinstance(table, TransitGatewayRouteTable)
    assert [link.attachment_id for link in table.propagations] == ["tgw-attach-data"]
    assert [route.route_type for route in table.routes] == [
        TransitGatewayRouteType.STATIC,
        TransitGatewayRouteType.PROPAGATED,
        TransitGatewayRouteType.STATIC,
    ]
    assert table.routes[2].state == "blackhole"
    assert table.routes_complete is True
    cited = snapshot.evidence[table.fact_evidence["/routes/2"][0]]
    assert cited.configuration_path == "Routes[2]"

    saved = save_snapshot(snapshot, tmp_path / "transit-gateway.json")
    assert load_snapshot(saved) == snapshot


@pytest.mark.parametrize(
    "fields",
    [
        {"attachment_ids": ["tgw-attach-app"], "state": "blackhole"},
        {"state": "active"},
    ],
    ids=["blackhole-with-attachment", "neither"],
)
def test_impossible_routes_are_rejected(fields):
    with pytest.raises(ValidationError):
        TransitGatewayRoute(destination="10.9.0.0/16", **fields)


def test_association_to_another_gateways_route_table_is_rejected():
    data = json.loads(FIXTURE.read_text())
    data["regions"]["eu-west-1"]["resources"][5]["transit_gateway_id"] = "tgw-other"
    with pytest.raises(SnapshotValidationError) as error:
        validate_snapshot(data)
    assert error.value.details[0].type == "snapshot_transit_gateway_link_mismatch"
