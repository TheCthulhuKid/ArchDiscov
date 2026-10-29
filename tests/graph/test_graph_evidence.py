from archdiscov.graph import GraphIndex
from archdiscov.models.enums import EvidenceValueStatus
from archdiscov.models.snapshot_access import upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_partial_fixture_fact_lookup_is_exact_not_prefix_or_source_path():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    resource_id = index.resource_ids[0]
    assert index.fact_evidence_ids(resource_id, "/attributes/ingress/0") == ("sg-rule",)
    (record,) = index.evidence_for_fact(resource_id, "/attributes/ingress/0")
    assert record.configuration_path == "SecurityGroups[0].IpPermissions[0]"
    assert record.value["FromPort"] == 443
    for pointer in (
        "/attributes/ingress",
        "/attributes/ingress/0/from_port",
        "SecurityGroups[0].IpPermissions[0]",
        "",
    ):
        assert index.fact_evidence_ids(resource_id, pointer) == ()
        assert index.evidence_for_fact(resource_id, pointer) == ()
    assert index.get_evidence("missing") is None
    assert index.evidence_for_resource("missing") == ()
    assert index.evidence_for_fact("missing", "/attributes/ingress/0") == ()


def test_known_null_unavailable_and_uncited_owned_evidence_stay_distinct():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    snapshot.evidence["uncited"] = snapshot.evidence["vm-size"].model_copy(deep=True)
    vm = snapshot.regions["eastus"].resources[0].children[0].children[0]
    assert "uncited" not in vm.evidence_ids
    index = GraphIndex(snapshot)
    (known,) = index.evidence_for_fact(vm.id, "/vm_size")
    assert known.value is None
    assert known.value_status == EvidenceValueStatus.PRESENT
    unavailable = index.get_evidence("vm-unavailable")
    assert unavailable.value is None
    assert unavailable.value_status == EvidenceValueStatus.UNRESOLVED
    assert unavailable.limitation == "Offline fixture does not provide this fact"
    assert index.resource_evidence_ids(vm.id) == (
        "uncited",
        "vm-attachment",
        "vm-size",
        "vm-unavailable",
    )
    assert index.fact_evidence_ids(vm.id, "/vm_size") == ("vm-size",)


def test_multiple_citations_keep_their_order_and_redactions():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "partial-coverage.json"))
    owner = snapshot.regions["eu-west-1"].resources[0]
    record = snapshot.evidence["sg-rule"].model_copy(deep=True)
    record.value = {"password": "protected-marker", "safe": [None]}
    snapshot.evidence["redacted"] = record
    owner.fact_evidence["/attributes/ingress/0"] = ["sg-rule", "redacted"]
    index = GraphIndex(snapshot)
    assert index.fact_evidence_ids(owner.id, "/attributes/ingress/0") == ("sg-rule", "redacted")
    redacted = index.get_evidence("redacted")
    assert redacted.value == {"password": "[REDACTED]", "safe": [None]}
    assert redacted.redacted_paths == ["/password"]
    assert record.value["password"] == "protected-marker"
    assert index.evidence_for_fact(owner.id, "/attributes/ingress/0")[1] == redacted


def test_input_and_all_returned_evidence_are_isolated():
    snapshot = load_snapshot(FIXTURES / "partial-coverage.json")
    index = GraphIndex(snapshot)
    owner = index.resource_ids[0]
    expected = index.get_evidence("sg-rule")
    snapshot.evidence["sg-rule"].value.clear()
    snapshot.evidence.clear()
    snapshot.regions[next(iter(snapshot.regions))].resources[0].fact_evidence.clear()
    for returned in (
        index.get_evidence("sg-rule"),
        index.evidence_for_resource(owner)[0],
        index.evidence_for_fact(owner, "/attributes/ingress/0")[0],
    ):
        returned.value.clear()
        returned.redacted_paths.append("/changed")
        returned.scope.account_id = "999999999999"
    assert index.get_evidence("sg-rule") == expected
    assert index.evidence_for_resource(owner) == (expected,)
    assert index.evidence_for_fact(owner, "/attributes/ingress/0") == (expected,)
