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


@pytest.mark.parametrize(
    "actions",
    [
        ["no-op"],
        ["create"],
        ["read"],
        ["update"],
        ["delete"],
        ["create", "delete"],
        ["delete", "create"],
    ],
)
def test_lifecycle_selects_values_and_exact_pointers(actions):
    entry = resource()
    entry["change"]["actions"] = actions
    if actions == ["create"]:
        entry["change"]["before"] = None
    if actions == ["delete"]:
        entry["change"]["after"] = None
    parsed = parse_plan(plan([entry]))
    actual = parsed.resources[0]
    assert actual.actions == [LifecycleAction(action) for action in actions]
    assert actual.path == "/resource_changes/0"
    assert actual.actions_path == "/resource_changes/0/change/actions"
    scope = "before" if actions == ["delete"] else "after"
    assert actual.values_path == f"/resource_changes/0/change/{scope}"
    assert actual.values == entry["change"][scope]
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
    }
    document = plan([entry], variables={"secret": {"value": SECRET}})
    original = deepcopy(document)
    parsed = parse_plan(document)
    actual = parsed.resources[0]
    assert actual.values == {
        "explicit_null": None,
        "tags": {"Name": "[REDACTED]", "known": "yes"},
        "password": "[REDACTED]",
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
    assert set(actual.redacted_paths) == {"/tags/Name", "/absent", "/password"}
    assert SECRET not in repr(asdict(parsed))
    assert document == original


def test_unknown_absent_subtrees_and_escaped_pointers():
    entry = resource()
    entry["change"].update(
        after={"known": None},
        after_unknown={"missing/tree~": {"child": True}, "list": [True, False]},
        after_sensitive={"missing/tree~": {"child": True}},
    )
    actual = parse_plan(plan([entry])).resources[0]
    assert actual.values == {"known": None}
    assert set(actual.unknown_paths) == {"/missing~1tree~0/child", "/list/0", "/list"}
    assert actual.redacted_paths == ["/missing~1tree~0/child"]


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
    assert actual.values == {"id": "[REDACTED]"}
    assert actual.redacted_paths == ["/id"]
    assert actual.unknown_paths == []


@pytest.mark.parametrize("mask", [True, {"tags": True}])
def test_sensitive_containers_cannot_hide_unknown_paths(mask):
    entry = resource()
    entry["change"].update(after_sensitive=mask, after_unknown={"tags": {"Name": True}})
    actual = parse_plan(plan([entry])).resources[0]
    assert "/tags/Name" in actual.unknown_paths
    assert SECRET not in repr(actual)
    if mask is True:
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
    "actions", [[], ["replace"], ["create", "update"], ["delete", "delete"], "create", [1], None]
)
def test_rejects_malformed_lifecycle_without_echoing_values(actions):
    entry = resource()
    entry["change"]["actions"] = actions
    with pytest.raises(IngestionError) as error:
        parse_plan(plan([entry]))
    assert error.value.code == "invalid_request"
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("after_unknown", 1),
        ("before_sensitive", SECRET),
        ("after_unknown", {"id": []}),
        ("after_sensitive", {"absent": True}),
        ("after_unknown", {"tags": {"Name": SECRET}}),
    ],
)
def test_rejects_incompatible_or_non_boolean_masks(key, value):
    entry = resource()
    entry["change"][key] = value
    with pytest.raises(IngestionError) as error:
        parse_plan(plan([entry]))
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": SECRET},
        {"type": "azurerm_virtual_network"},
        {"address": " "},
        {"change": None},
        {"change": {"actions": ["update"], "after": None}},
    ],
)
def test_rejects_bad_managed_changes(overrides):
    with pytest.raises(IngestionError) as error:
        parse_plan(plan([resource(**overrides)]))
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"format_version": "2.0"},
        {"terraform_version": SECRET},
        {"resource_changes": {}},
        {"planned_values": {"root_module": []}},
        {"version": 4},
    ],
)
def test_rejects_bad_documents(overrides):
    with pytest.raises(IngestionError) as error:
        parse_plan(plan(**overrides))
    assert SECRET not in str(error.value)


def test_rejects_state_missing_plan_inventory_and_duplicate_addresses():
    for document in [None, [], {"format_version": "1.0"}, plan([resource(), resource()])]:
        with pytest.raises(IngestionError):
            parse_plan(document)


def test_empty_plan_and_inventory_only_are_valid():
    assert parse_plan(plan([])).resources == []
    assert parse_plan({"format_version": "1.0", "planned_values": {}}).resources == []


def test_load_plan_uses_strict_shared_json_loader(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()), encoding="utf-8")
    assert load_plan(path) == parse_plan(plan())
    for raw in ['{"format_version":"1.0","format_version":"1.0"}', '{"x":NaN}', SECRET]:
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(IngestionError) as error:
            load_plan(path)
        assert SECRET not in str(error.value)
