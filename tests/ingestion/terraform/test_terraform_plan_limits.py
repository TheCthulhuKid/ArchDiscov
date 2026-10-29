import json

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform.sensitivity import read_pointer
from tests.support.terraform_data import change, collect, resources, write_plan


def test_inventory_only_noop_has_no_invented_actions_source(tmp_path):
    inventory = {
        "root_module": {
            "resources": [
                {
                    "address": "aws_instance.existing",
                    "type": "aws_instance",
                    "mode": "managed",
                    "provider_name": "registry.terraform.io/hashicorp/aws",
                    "values": {"id": "i-existing", "instance_type": "t3.micro"},
                    "sensitive_values": {},
                }
            ]
        }
    }
    path = write_plan(tmp_path, [], planned_values=inventory)
    snapshot = collect(path)
    resource = resources(snapshot)["aws_instance.existing"]
    assert resource.lifecycle.actions == ["no-op"] and resource.lifecycle.value_scope == "after"
    assert "/lifecycle/actions" not in resource.fact_evidence
    raw = json.loads(path.read_text())
    for record in snapshot.evidence.values():
        read_pointer(raw, record.configuration_path)


@pytest.mark.parametrize(
    "extra,path,empty",
    [
        ({"complete": False}, "/complete", True),
        (
            {
                "deferred_changes": [
                    {
                        "reason": "secret-reason",
                        "resource_change": {"after": {"secret": "never-copy-deferred"}},
                    }
                ]
            },
            "/deferred_changes",
            False,
        ),
    ],
    ids=["incomplete-empty-plan", "deferred-changes"],
)
def test_partial_plan_status_cannot_be_successful_absence(tmp_path, extra, path, empty):
    snapshot = collect(write_plan(tmp_path, [] if empty else [change()], **extra))
    assert snapshot.coverage.operations
    assert all(operation.status == "incomplete" for operation in snapshot.coverage.operations)
    assert all(
        any(value.configuration_path == path for value in operation.unresolved_values)
        for operation in snapshot.coverage.operations
    )
    assert "secret-reason" not in snapshot.model_dump_json()
    assert "never-copy-deferred" not in snapshot.model_dump_json()


def test_errored_plan_status_rejected(tmp_path):
    with pytest.raises(IngestionError, match="plan"):
        collect(write_plan(tmp_path, [change()], errored=True))


def test_empty_plan_is_success_zero_only_with_scope(tmp_path):
    snapshot = collect(write_plan(tmp_path, []))
    assert len(snapshot.coverage.operations) == 1
    assert snapshot.coverage.operations[0].status == "success"
    assert snapshot.coverage.operations[0].result_count == 0
