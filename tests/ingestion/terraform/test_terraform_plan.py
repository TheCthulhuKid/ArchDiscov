import asyncio
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform import TerraformPlanAdapter, TerraformPlanRequest
from archdiscov.models.architecture import ArchitectureSnapshot, ResourceLifecycle
from archdiscov.models.enums import EvidenceValueStatus, LifecycleValueScope
from archdiscov.services import (
    SnapshotConflictError,
    collect_snapshots,
    collect_snapshots_async,
    get_evidence,
    load_snapshot,
    save_snapshot,
)
from tests.support.terraform_data import (
    ACCOUNT,
    CONTEXT,
    FIXTURE,
    change,
    collect,
    plan_request,
    resources,
    write_plan,
)


def test_plan_fixture_lifecycle_uncertainty_persistence_and_evidence(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline plan import must not execute or contact providers")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("boto3.Session", forbidden)
    snapshot = collect(FIXTURE)
    items = resources(snapshot)
    assert snapshot.schema_version == "2" and snapshot.source.kind == "terraform_plan"
    assert snapshot.source.format_version == "1.2"
    assert len(items) == 6
    updated = items['module.app.aws_instance.web["blue/green"]']
    assert updated.instance_type == "t3.small"
    assert "subnet_id" not in updated.attributes
    assert "/attributes/subnet_id" not in updated.fact_evidence
    deleted = items["aws_instance.removed"]
    assert deleted.lifecycle.actions == ["delete"]
    assert deleted.lifecycle.value_scope == LifecycleValueScope.BEFORE
    assert deleted.identity.physical_id == "i-delete" and deleted.instance_type == "t3.nano"
    for address, actions in (
        ("aws_instance.replace_before", ["delete", "create"]),
        ("aws_instance.replace_after", ["create", "delete"]),
    ):
        replacement = items[address]
        assert replacement.lifecycle.actions == actions
        assert replacement.lifecycle.value_scope == "after"
        assert replacement.identity.physical_id is None
        assert replacement.identity.logical_address == address
    new_subnet = items["module.app.aws_subnet.main[0]"]
    assert new_subnet.identity.physical_id is None
    assert items["aws_vpc.main"].children == [new_subnet]
    fact = updated.fact_evidence["/instance_type"][0]
    evidence = snapshot.evidence[fact]
    assert evidence.configuration_path == "/resource_changes/2/change/after/instance_type"
    assert evidence.value == "t3.small"
    assert (
        snapshot.evidence[deleted.fact_evidence["/instance_type"][0]].configuration_path
        == "/resource_changes/3/change/before/instance_type"
    )
    assert any(
        record.value_status == EvidenceValueStatus.UNRESOLVED
        and record.configuration_path == "/resource_changes/2/change/after_unknown/subnet_id"
        for record in snapshot.evidence.values()
    )
    assert any(
        value.configuration_path.endswith("/after_unknown/id")
        for operation in snapshot.coverage.operations
        for value in operation.unresolved_values
    )
    output = save_snapshot(snapshot, tmp_path / "snapshot.json")
    loaded = load_snapshot(output)
    assert loaded == snapshot
    assert get_evidence(loaded, fact).evidence == evidence
    saved = output.read_text()
    for secret in (
        "sensitive-plan-tag",
        "sensitive-before-tag",
        "plan-variable-secret",
        "plan-output-secret",
        "i-old",
        "t2.micro",
    ):
        assert secret not in saved
    with pytest.raises(SnapshotConflictError):
        save_snapshot(snapshot, output)
    assert output.read_text() == saved
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_plan_async_and_sync_api_agree():
    assert asyncio.run(collect_snapshots_async(plan_request(FIXTURE), context=CONTEXT)) == [
        collect(FIXTURE)
    ]


@pytest.mark.parametrize(
    "values,unknown,has_citation",
    [
        ({}, {}, False),
        ({"instance_type": None}, {}, True),
        ({"instance_type": None}, {"instance_type": True}, False),
        ({"instance_type": "stale"}, {"instance_type": True}, False),
    ],
)
def test_missing_null_and_unknown_are_distinct(tmp_path, values, unknown, has_citation):
    snapshot = collect(write_plan(tmp_path, [change(after=values, after_unknown=unknown)]))
    resource = next(iter(resources(snapshot).values()))
    assert resource.instance_type is None
    assert ("/instance_type" in resource.fact_evidence) is has_citation
    unknown_evidence = [
        item for item in snapshot.evidence.values() if item.value_status == "unresolved"
    ]
    assert bool(unknown_evidence) == bool(unknown)
    if has_citation:
        record = snapshot.evidence[resource.fact_evidence["/instance_type"][0]]
        assert record.value is None and record.value_status == "present"


def test_before_never_backfills_unknown_after_values(tmp_path):
    snapshot = collect(
        write_plan(
            tmp_path,
            [
                change(
                    actions=["update"],
                    before={"id": "i-before", "instance_type": "old"},
                    after={},
                    after_unknown={"id": True, "instance_type": True},
                )
            ],
        )
    )
    resource = next(iter(resources(snapshot).values()))
    assert resource.identity.physical_id is None and resource.instance_type is None
    assert "/instance_type" not in resource.fact_evidence
    assert (
        "i-before" not in snapshot.model_dump_json() and '"old"' not in snapshot.model_dump_json()
    )


@pytest.mark.parametrize(
    "before",
    [
        {"owner_id": "999999999999"},
        {"arn": "arn:aws:ec2:us-east-1:999999999999:instance/i-before"},
        {"arn": "arn:aws:ec2:us-east-1:123456789012:instance/i-before", "region": "eu-west-1"},
        {"account_id": "invalid-account"},
        {"arn": "arn:malformed"},
    ],
)
def test_before_context_conflicts_rejected_without_disclosing_values(tmp_path, before):
    path = write_plan(
        tmp_path, [change(actions=["update"], before=before, after={"instance_type": "t3.micro"})]
    )
    with pytest.raises(IngestionError, match="context") as caught:
        collect(path)
    assert "999999999999" not in str(caught.value)


def test_before_context_still_rejects_unsupported_regions(tmp_path):
    path = write_plan(
        tmp_path, [change(actions=["update"], before={"region": "unsupported-region"})]
    )
    with pytest.raises(IngestionError, match="Unsupported Terraform resource region") as caught:
        collect(path)
    assert caught.value.code == "unsupported_region"
    assert "unsupported-region" not in str(caught.value)


@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
@pytest.mark.parametrize("explicit_region", [False, True])
def test_replacement_can_move_regions_without_reusing_before_context(
    tmp_path, actions, explicit_region
):
    path = write_plan(
        tmp_path,
        [
            change(
                actions=actions,
                before={"arn": f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/i-before"},
                after={"arn": f"arn:aws:ec2:eu-west-1:{ACCOUNT}:instance/i-after"},
            )
        ],
    )
    snapshot = collect_snapshots(
        TerraformPlanRequest(
            path=path, account_id=ACCOUNT, region="eu-west-1" if explicit_region else None
        ),
        context=CONTEXT,
    )[0]
    resource = resources(snapshot)["aws_instance.example"]
    assert resource.identity.region == "eu-west-1"
    assert resource.lifecycle.actions == actions
    assert resource.lifecycle.value_scope == "after"
    assert "us-east-1" not in snapshot.model_dump_json()
    assert "i-before" not in snapshot.model_dump_json()


def test_unknowns_require_scope_rather_than_inferring_prior_state(tmp_path):
    path = write_plan(
        tmp_path,
        [
            change(
                actions=["update"],
                before={"arn": "arn:aws:ec2:eu-west-1:123456789012:instance/i-old"},
                after={},
                after_unknown={"arn": True},
            )
        ],
    )
    with pytest.raises(IngestionError, match="account_id"):
        collect_snapshots(TerraformPlanRequest(path=path), context=CONTEXT)


@pytest.mark.parametrize(
    "actions,scope",
    [(["delete"], "after"), (["create"], "before"), (["delete", "create"], "before")],
)
def test_lifecycle_scope_must_match_actions(actions, scope):
    with pytest.raises(ValidationError, match="value_scope"):
        ResourceLifecycle(actions=actions, value_scope=scope)


def test_action_only_historical_lifecycle_remains_compatible():
    lifecycle = ResourceLifecycle(actions=["delete"])
    assert lifecycle.model_dump() == {"actions": ["delete"]}


def test_request_rejects_global_and_adapter_rejects_wrong_kind():
    from archdiscov.ingestion.terraform import TerraformStateRequest

    with pytest.raises(ValidationError, match="concrete"):
        TerraformPlanRequest(path=FIXTURE, region="global")
    with pytest.raises(IngestionError, match="TerraformPlanRequest"):
        TerraformPlanAdapter().collect(TerraformStateRequest(path=FIXTURE), context=CONTEXT)


def test_input_data_unchanged(tmp_path):
    data = json.loads(FIXTURE.read_text())
    original = deepcopy(data)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data))
    collect(path)
    assert json.loads(path.read_text()) == original
