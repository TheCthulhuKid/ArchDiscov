import pytest
from pydantic import ValidationError

from archdiscov.models.enums import ResourceType
from archdiscov.models.resources.ecs import (
    EcsCluster,
    EcsContainerDefinition,
    EcsService,
    EcsTask,
    EcsTaskDefinition,
)
from tests.support.ecs_data import ACCOUNT, CLUSTER, DEFINITION, REGION, SERVICE, TASK


@pytest.mark.parametrize(
    "cls,arn,kind",
    [
        (EcsCluster, CLUSTER, ResourceType.ECS_CLUSTER),
        (EcsService, SERVICE, ResourceType.ECS_SERVICE),
        (EcsTask, TASK, ResourceType.ECS_TASK),
        (EcsTaskDefinition, DEFINITION, ResourceType.ECS_TASK_DEFINITION),
    ],
)
def test_required_arn_is_the_physical_identity(cls, arn, kind):
    identity = dict(account_id=ACCOUNT, region=REGION, service="ecs", physical_id=arn)
    resource = cls(identity=identity, arn=arn)
    assert resource.resource_type == kind
    assert resource.id == resource.identity.stable_id
    assert not resource.children
    with pytest.raises(ValidationError):
        cls(identity=identity)
    with pytest.raises(ValidationError):
        cls(identity=identity, arn=arn + "other")
    with pytest.raises(ValidationError):
        cls(identity={**identity, "service": "ec2"}, arn=arn)


@pytest.mark.parametrize(
    "field", ["environment", "command", "entry_point", "secrets", "description"]
)
def test_container_models_have_no_workload_content_fields(field):
    with pytest.raises(ValidationError):
        EcsContainerDefinition(**{field: "private-content"})


def test_absent_network_is_unknown_not_an_empty_inventory():
    service = EcsService(
        identity=dict(account_id=ACCOUNT, region=REGION, service="ecs", physical_id=SERVICE),
        arn=SERVICE,
    )
    assert service.subnet_ids is None
    assert service.security_group_ids is None
    task = EcsTask(
        identity=dict(account_id=ACCOUNT, region=REGION, service="ecs", physical_id=TASK), arn=TASK
    )
    assert task.network_interface_ids is None
