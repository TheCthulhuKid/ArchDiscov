import pytest

from archdiscov.ingestion.adapter import IngestionError
from tests.support.terraform_data import change, collect, resources, write_plan


def configuration(reference, address="aws_instance.example"):
    return {
        "root_module": {
            "resources": [
                {"address": address, "expressions": {"subnet_id": {"references": [reference]}}}
            ]
        }
    }


def test_configuration_reference_resolves_logical_new_target_without_physical_id(tmp_path):
    path = write_plan(
        tmp_path,
        [change(), change("aws_subnet.main", after={"cidr_block": "10.0.1.0/24"})],
        configuration=configuration("aws_subnet.main.id"),
    )
    snapshot = collect(path)
    items = resources(snapshot)
    dependency = snapshot.dependencies_by_region["eu-west-1"][0]
    assert dependency.source_id == items["aws_instance.example"].id
    assert dependency.target.kind == "local"
    assert dependency.target.resource_id == items["aws_subnet.main"].id
    assert dependency.attributes["kind"] == "terraform_configuration_reference"
    record = snapshot.evidence[dependency.evidence_ids[0]]
    assert record.resource_id == dependency.source_id
    assert (
        record.configuration_path
        == "/configuration/root_module/resources/0/expressions/subnet_id/references/0"
    )
    assert record.value == "aws_subnet.main.id"


def test_unavailable_expressions_have_safe_evidence_and_coverage(tmp_path):
    reference = "var.subnet"
    snapshot = collect(write_plan(tmp_path, [change()], configuration=configuration(reference)))
    dependency = snapshot.dependencies_by_region["eu-west-1"][0]
    assert dependency.target.kind == "unresolved"
    record = snapshot.evidence[dependency.evidence_ids[0]]
    assert record.value is None and record.value_status == "unresolved"
    assert snapshot.coverage.operations[0].status == "incomplete"
    assert any(
        item.configuration_path == record.configuration_path
        for item in snapshot.coverage.operations[0].unresolved_values
    )
    assert reference not in snapshot.model_dump_json()


def test_indexed_target_reference_is_exact_and_unindexed_ambiguous_stays_unresolved(tmp_path):
    changes = [change(), change("aws_subnet.main[0]"), change("aws_subnet.main[1]")]
    snapshot = collect(
        write_plan(tmp_path, changes, configuration=configuration("aws_subnet.main[1].id"))
    )
    dependency = snapshot.dependencies_by_region["eu-west-1"][0]
    assert dependency.target.resource_id == resources(snapshot)["aws_subnet.main[1]"].id
    snapshot = collect(
        write_plan(tmp_path, changes, configuration=configuration("aws_subnet.main.id"))
    )
    assert snapshot.dependencies_by_region["eu-west-1"][0].target.kind == "unresolved"


def test_nested_module_references_use_local_configuration_scope(tmp_path):
    changes = [
        change("module.app.aws_instance.web[0]"),
        change("module.app.aws_subnet.main", after={"cidr_block": "10.0.1.0/24"}),
    ]
    config = {
        "root_module": {
            "module_calls": {
                "app": {
                    "module": configuration("aws_subnet.main.id", address="aws_instance.web")[
                        "root_module"
                    ]
                }
            }
        }
    }
    snapshot = collect(write_plan(tmp_path, changes, configuration=config))
    dependency = snapshot.dependencies_by_region["eu-west-1"][0]
    assert dependency.target.resource_id == resources(snapshot)["module.app.aws_subnet.main"].id
    assert snapshot.evidence[dependency.evidence_ids[0]].configuration_path.startswith(
        "/configuration/root_module/module_calls/app/module/"
    )


def test_deleted_parent_cannot_contain_or_resolve_planned_after_child(tmp_path):
    deleted = change("aws_vpc.old", actions=["delete"], before={"id": "vpc-old"})
    deleted["change"]["after"] = None
    path = write_plan(
        tmp_path,
        [deleted, change("aws_subnet.new", after={"vpc_id": "vpc-old"})],
        configuration=configuration("aws_vpc.old.id", address="aws_subnet.new"),
    )
    snapshot = collect(path)
    assert len(snapshot.regions["eu-west-1"].resources) == 2
    assert all(not item.children for item in snapshot.regions["eu-west-1"].resources)
    assert all(
        dependency.target.kind == "unresolved"
        for dependency in snapshot.dependencies_by_region["eu-west-1"]
    )


def test_malformed_configuration_fails_safely(tmp_path):
    with pytest.raises(IngestionError, match="configuration"):
        collect(write_plan(tmp_path, [change()], configuration={"root_module": []}))


def test_configuration_source_index_keeps_all_normalized_instances(tmp_path):
    addresses = ["aws_instance.example[0]", "aws_instance.example[1]"]
    changes = [*(change(address) for address in addresses), change("aws_subnet.main")]
    snapshot = collect(
        write_plan(tmp_path, changes, configuration=configuration("aws_subnet.main.id"))
    )
    items = resources(snapshot)
    dependencies = snapshot.dependencies_by_region["eu-west-1"]
    assert [link.source_id for link in dependencies] == [items[address].id for address in addresses]
    assert all(link.target.resource_id == items["aws_subnet.main"].id for link in dependencies)
