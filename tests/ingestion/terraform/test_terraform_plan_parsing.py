import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform.plan_parsing import load_plan, parse_plan
from archdiscov.models.enums import LifecycleAction

AWS_PROVIDER = "registry.terraform.io/hashicorp/aws"
SECRET = "never-expose-this-plan-secret"


def resource(address="aws_vpc.main", **overrides):
    entry = {
        "address": address,
        "mode": "managed",
        "type": "aws_vpc",
        "provider_name": AWS_PROVIDER,
        "change": {
            "actions": ["update"],
            "before": {"id": "vpc-old"},
            "after": {"id": "vpc-new", "tags": {"Name": "planned"}},
            "after_unknown": {},
            "before_sensitive": {},
            "after_sensitive": {},
        },
    }
    entry.update(overrides)
    return entry


def plan(entries=None, **overrides):
    document = {
        "format_version": "1.2",
        "terraform_version": "1.9.8",
        "resource_changes": [resource()] if entries is None else entries,
    }
    document.update(overrides)
    return document


def test_lifecycle_selects_after_values_and_exact_pointers():
    entry = resource()
    parsed = parse_plan(plan([entry]))
    actual = parsed.resources[0]
    assert actual.actions == [LifecycleAction.UPDATE]
    assert actual.path == "/resource_changes/0"
    assert actual.actions_path == "/resource_changes/0/change/actions"
    assert actual.values_path == "/resource_changes/0/change/after"
    assert actual.values == entry["change"]["after"]
    assert actual.terraform_type == "aws_vpc"
    assert actual.address == "aws_vpc.main"
    assert parsed.format_version == "1.2"


def test_nested_unknown_sensitive_null_and_missing_are_distinct_and_do_not_mutate():
    entry = resource()
    entry["change"]["after"] = {
        "id": None,
        "explicit_null": None,
        "tags": {"Name": SECRET, "known": "yes", "unknown": None},
        "password": SECRET,
        "nested": [{"known": "yes", "unknown": None}],
        "groups": ["sg-first", None, "sg-last"],
        "names": ["public", SECRET],
    }
    entry["change"]["after_unknown"] = {
        "id": True,
        "absent": True,
        "tags": {"unknown": True},
        "nested": [{"unknown": True}],
        "groups": [False, True, False],
    }
    entry["change"]["after_sensitive"] = {
        "tags": {"Name": True},
        "absent": True,
        "names": [False, True],
    }
    document = plan([entry], variables={"secret": {"value": SECRET}})
    original = deepcopy(document)
    parsed = parse_plan(document)
    actual = parsed.resources[0]
    assert actual.values == {
        "explicit_null": None,
        "tags": {"Name": "[REDACTED]", "known": "yes"},
        "password": "[REDACTED]",
        "names": ["public", "[REDACTED]"],
    }
    assert set(actual.unknown_paths) == {
        "/id",
        "/absent",
        "/tags/unknown",
        "/nested/0/unknown",
        "/nested",
        "/groups/1",
        "/groups",
    }
    assert set(actual.redacted_paths) == {"/tags/Name", "/absent", "/password", "/names/1"}
    assert SECRET not in repr(asdict(parsed))
    assert document == original


def test_before_only_retains_sanitized_scope_context_and_never_fills_unknown():
    entry = resource()
    entry["change"].update(
        before={
            "id": "vpc-old",
            "region": "us-east-1",
            "account_id": "123456789012",
            "arn": SECRET,
            "tags": {"Name": SECRET},
        },
        before_sensitive={"arn": True},
        after={},
        after_unknown={"id": True},
    )
    actual = parse_plan(plan([entry])).resources[0]
    assert actual.values == {}
    assert actual.before_values == {
        "id": "vpc-old",
        "region": "us-east-1",
        "account_id": "123456789012",
        "arn": "[REDACTED]",
    }
    assert SECRET not in repr(asdict(actual))


def test_delete_applies_before_sensitive_not_after_sensitive():
    entry = resource()
    entry["change"].update(
        actions=["delete"], before={"id": SECRET}, after=None, before_sensitive={"id": True}
    )
    actual = parse_plan(plan([entry])).resources[0]
    assert actual.values_path == "/resource_changes/0/change/before"
    assert actual.values == {"id": "[REDACTED]"}
    assert actual.redacted_paths == ["/id"]
    assert actual.unknown_paths == []


def test_sensitive_containers_cannot_hide_unknown_paths():
    entry = resource()
    entry["change"].update(after_sensitive=True, after_unknown={"tags": {"Name": True}})
    actual = parse_plan(plan([entry])).resources[0]
    assert "/tags/Name" in actual.unknown_paths
    assert actual.values == {}
    assert actual.redacted_paths == [""]


def test_merges_nested_planned_inventory_without_overwriting_authoritative_changes():
    unchanged = resource("module.child.aws_vpc.other")
    unchanged.pop("change")
    unchanged.update(values={"id": "vpc-other"}, sensitive_values={})
    duplicate = resource()
    duplicate.pop("change")
    duplicate.update(values={"id": "not-authoritative"}, sensitive_values={})
    parsed = parse_plan(
        plan(
            planned_values={
                "root_module": {
                    "resources": [duplicate],
                    "child_modules": [{"resources": [unchanged]}],
                }
            }
        )
    )
    assert len(parsed.resources) == 2
    changed, implicit = parsed.resources
    assert changed.values["id"] == "vpc-new"
    assert implicit.values == {"id": "vpc-other"}
    assert implicit.actions == [LifecycleAction.NO_OP]
    assert implicit.actions_path is None
    assert implicit.values_path == (
        "/planned_values/root_module/child_modules/0/resources/0/values"
    )


def test_changed_inventory_can_omit_unknown_sensitive_fields():
    entry = resource()
    entry["change"].update(after={}, after_unknown={"id": True}, after_sensitive={"id": True})
    inventory = resource()
    inventory.pop("change")
    inventory.update(values={}, sensitive_values={"id": True})
    parsed = parse_plan(
        plan(
            [entry],
            planned_values={
                "root_module": {
                    "resources": [inventory],
                }
            },
        )
    )
    assert parsed.resources[0].values == {}
    assert parsed.resources[0].unknown_paths == ["/id"]


def test_skips_data_resources_in_both_inventory_and_changes():
    data = resource("data.aws_vpc.main", mode="data")
    parsed = parse_plan(
        plan(
            [data],
            planned_values={
                "root_module": {
                    "resources": [{"mode": "data"}],
                }
            },
        )
    )
    assert parsed.resources == []
    assert parsed.skipped_paths == [
        "/resource_changes/0",
        "/planned_values/root_module/resources/0",
    ]


@pytest.mark.parametrize(
    "entry_fields,change_fields",
    [
        ({}, {"actions": ["create", SECRET]}),
        ({}, {"after_unknown": {"id": SECRET}}),
        ({}, {"after": SECRET}),
        ({"deposed": SECRET}, {}),
    ],
    ids=["lifecycle", "non-boolean-mask", "scope-values-not-object", "deposed"],
)
def test_rejects_bad_managed_changes_without_echoing_values(entry_fields, change_fields):
    entry = resource(**entry_fields)
    entry["change"].update(change_fields)
    with pytest.raises(IngestionError) as error:
        parse_plan(plan([entry]))
    assert error.value.code == "invalid_request"
    assert SECRET not in str(error.value)


def test_rejects_internal_state_documents():
    with pytest.raises(IngestionError, match="not state or internal state"):
        parse_plan(plan(version=4))


def test_empty_plan_and_inventory_only_are_valid():
    assert parse_plan(plan([])).resources == []
    assert parse_plan({"format_version": "1.0", "planned_values": {}}).resources == []


def test_load_plan_reads_the_public_plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()), encoding="utf-8")
    assert load_plan(path) == parse_plan(plan())
