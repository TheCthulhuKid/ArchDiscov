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
    import json

    raw = json.loads(path.read_text())
    for record in snapshot.evidence.values():
        read_pointer(raw, record.configuration_path)


@pytest.mark.parametrize(
    "extra,path",
    [
        ({"complete": False}, "/complete"),
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
        ),
    ],
)
@pytest.mark.parametrize("empty", [False, True])
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


@pytest.mark.parametrize(
    "extra",
    [{"errored": True}, {"errored": "secret-error"}, {"complete": 0}, {"deferred_changes": {}}],
)
def test_invalid_or_errored_plan_status_rejected_safely(tmp_path, extra):
    with pytest.raises(IngestionError, match=r"plan|deferred_changes") as caught:
        collect(write_plan(tmp_path, [change()], **extra))
    assert "secret-error" not in str(caught.value)


def test_empty_plan_is_success_zero_only_with_scope(tmp_path):
    snapshot = collect(write_plan(tmp_path, []))
    assert len(snapshot.coverage.operations) == 1
    assert snapshot.coverage.operations[0].status == "success"
    assert snapshot.coverage.operations[0].result_count == 0
