"""README acceptance scenarios that no single existing test asserts end to end.

Terraform module addresses, planned deletions, unknown values, CloudFormation unresolved
references, and the safe matching of observed and declared resources, through the real
ingestion services and the comparison, asserting each scenario's qualified outcome.
"""

import json
from urllib.parse import unquote

import pytest

from archdiscov.services import compare_snapshots, ingest_snapshot, load_snapshot
from tests.comparison.snapshots import aws_resource, aws_snapshot

ACCOUNT, REGION = "123456789012", "eu-west-1"
MODULE_INSTANCE = "module.app.aws_instance.web"
MODULE_SUBNET = "module.app.aws_subnet.main[0]"


def change(address, actions, before, after, unknown):
    return {
        "address": address,
        "type": address.split(".")[-2].split("[")[0],
        "mode": "managed",
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": unknown,
        },
    }


def ingest(tmp_path, name, document, source_format, **options):
    source = tmp_path / f"{name}-source.json"
    source.write_text(json.dumps(document))
    output = tmp_path / f"{name}.json"
    ingest_snapshot(
        source, output, source_format=source_format, account_id=ACCOUNT, region=REGION, **options
    )
    return load_snapshot(output)


def instance(physical, **fields):
    return {**aws_resource("AWS::EC2::Instance", physical=physical), **fields}


@pytest.fixture
def declared_against_observed(tmp_path):
    plan = {
        "format_version": "1.2",
        "planned_values": {"root_module": {}},
        "resource_changes": [
            change(
                MODULE_INSTANCE,
                ["update"],
                {"id": "i-update", "instance_type": "t3.micro"},
                {"id": "i-update"},
                {"instance_type": True},
            ),
            change(
                "aws_instance.gone",
                ["delete"],
                {"id": "i-delete", "instance_type": "t3.nano"},
                None,
                False,
            ),
            change(
                MODULE_SUBNET,
                ["create"],
                None,
                {"cidr_block": "10.0.1.0/24", "tags": {"Name": "shared-name"}},
                {"id": True},
            ),
        ],
    }
    declared = ingest(tmp_path, "plan", plan, "terraform-plan")
    observed = aws_snapshot(
        [
            instance("i-update", instance_type="t3.micro"),
            instance("i-delete", instance_type="t3.nano"),
            instance("i-extra"),
            {
                **aws_resource("AWS::EC2::Subnet", physical="subnet-live"),
                "attributes": {"tags": {"Name": "shared-name"}},
            },
        ],
        seed=1,
    )
    return observed, declared, compare_snapshots(observed, declared)


def kinds(entry):
    return {str(q.kind) for q in entry.qualifications}


def test_module_addresses_survive_import_and_a_planned_resource_stays_planned(
    declared_against_observed,
):
    _, declared, result = declared_against_observed
    addresses = {
        r.identity.logical_address for tree in declared.regions.values() for r in tree.resources
    }
    assert {MODULE_INSTANCE, MODULE_SUBNET} <= addresses
    [planned] = [u for u in result.unmatched if str(u.side) == "right"]
    assert unquote(planned.resource.resource_id).endswith(MODULE_SUBNET)
    planned_actions = [q.lifecycle.actions for q in planned.qualifications if q.lifecycle]
    assert planned_actions == [["create"]]


def test_a_planned_deletion_is_a_planned_lifecycle_never_an_observed_absence(
    declared_against_observed,
):
    _, _, result = declared_against_observed
    pair = next(p for p in result.matched if p.left.resource_id.endswith("i-delete"))
    [lifecycle] = [q for q in pair.qualifications if q.lifecycle]
    assert (str(lifecycle.side), lifecycle.lifecycle.actions) == ("right", ["delete"])
    assert not any(u.resource.resource_id.endswith("i-delete") for u in result.unmatched)


def test_an_unknown_planned_value_is_indeterminate_not_a_change(declared_against_observed):
    _, _, result = declared_against_observed
    pair = next(p for p in result.matched if p.left.resource_id.endswith("i-update"))
    [difference] = [d for d in pair.differences if d.path == "/instance_type"]
    assert str(difference.kind) == "indeterminate"
    assert difference.left_value is None and difference.right_value is None
    [qualification] = [q for q in difference.qualifications if str(q.kind) == "unknown_value"]
    assert (str(qualification.side), qualification.evidence_ids != []) == ("right", True)
    assert not [d for d in pair.differences if "terraform_type" in d.path]


def test_observed_and_declared_resources_are_matched_only_by_identity(declared_against_observed):
    observed, _, result = declared_against_observed
    # The planned subnet shares a name tag with a live one but has no physical identity.
    assert {p.left.resource_id.rsplit("/", 1)[-1] for p in result.matched} == {
        "i-update",
        "i-delete",
    }
    assert all(str(p.basis) == "physical_identity" for p in result.matched)
    left_only = {
        u.resource.resource_id.rsplit("/", 1)[-1] for u in result.unmatched if str(u.side) == "left"
    }
    assert left_only == {"i-extra", "subnet-live"}
    # An observed resource missing from an incomplete plan is a qualified absence, not a removal.
    extra = next(u for u in result.unmatched if u.resource.resource_id.endswith("i-extra"))
    assert kinds(extra) <= {"limited_coverage", "unrecorded_coverage"} and kinds(extra)
    assert {str(item.kind) for item in result.limitations} == {"source_kind_mismatch"}
    assert observed.snapshot_id == result.left.snapshot_id


TEMPLATE = {
    "Conditions": {"Off": {"Fn::Equals": ["yes", "no"]}},
    "Resources": {
        "Omitted": {"Type": "AWS::EC2::VPC", "Condition": "Off"},
        "Role": {"Type": "AWS::IAM::Role"},
        "Function": {
            "Type": "AWS::Lambda::Function",
            "Properties": {"Role": {"Fn::GetAtt": ["Role", "Arn"]}},
        },
    },
}


def test_cloudformation_unresolved_references_are_indeterminate_between_two_templates(tmp_path):
    resolved = ingest(tmp_path, "resolved", TEMPLATE, "cloudformation", stack_name="demo")
    unresolved_template = json.loads(json.dumps(TEMPLATE))
    unresolved_template["Resources"]["Function"]["Properties"]["VpcId"] = {"Ref": "Omitted"}
    unresolved = ingest(
        tmp_path, "unresolved", unresolved_template, "cloudformation", stack_name="demo"
    )
    assert any(
        d.target.kind == "unresolved"
        for links in unresolved.dependencies_by_region.values()
        for d in links
    )
    result = compare_snapshots(resolved, unresolved)
    function = next(p for p in result.matched if p.left.resource_id.endswith("Function"))
    assert all(str(p.basis) == "logical_address" for p in result.matched)
    indeterminate = [d for d in function.differences if str(d.kind) == "indeterminate"]
    assert indeterminate
    for difference in indeterminate:
        assert difference.subject == "dependency"
        [qualification] = [
            q for q in difference.qualifications if str(q.kind) == "unresolved_reference"
        ]
        assert str(qualification.side) == "right"
    assert not [d for d in function.differences if str(d.kind) in {"added", "removed"}]
