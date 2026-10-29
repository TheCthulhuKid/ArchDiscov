import json
from copy import deepcopy

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.operations import EcsOperation as Op
from archdiscov.models.resources.ecs import EcsCluster, EcsService, EcsTask, EcsTaskDefinition
from tests.support.ecs_data import (
    ACCOUNT,
    CLUSTER,
    DEFINITION,
    PREFIX,
    REGION,
    SERVICE,
    collect,
    responses,
)


def coverage(result, operation):
    return next(item for item in result.coverage if item.operation == operation)


def denied(operation):
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "failure-private"}}, operation)


def test_all_stages_and_typed_configuration():
    result, session = collect()
    assert isinstance(result, ServiceCollection)
    assert {type(resource) for resource in result.resources} == {
        EcsCluster,
        EcsService,
        EcsTask,
        EcsTaskDefinition,
    }
    assert len(result.coverage) == len(Op) == 7
    assert all(item.status == "success" for item in result.coverage)
    assert session.clients[0][1]["region_name"] == "us-east-1"
    assert [params for method, params in session.ecs.calls if method == "list_tasks"] == [
        {"cluster": CLUSTER, "desiredStatus": "RUNNING"},
    ]
    assert sum(method == "describe_task_definition" for method, _ in session.ecs.calls) == 1
    service = next(resource for resource in result.resources if isinstance(resource, EcsService))
    assert service.subnet_ids == ["subnet-a"]
    assert service.security_group_ids == ["sg-a"]
    task = next(resource for resource in result.resources if isinstance(resource, EcsTask))
    assert task.network_interface_ids == ["eni-a"]
    assert task.subnet_ids == ["subnet-a"]
    definition = next(r for r in result.resources if isinstance(r, EcsTaskDefinition))
    assert definition.containers[0].image == "registry.example/web:1"
    assert definition.containers[0].port_mappings[0].container_port == 8080


def test_batch_limits():
    data = responses()
    clusters = [PREFIX + f"cluster/c{i:03}" for i in range(101)]
    services = [PREFIX + f"service/c000/s{i:03}" for i in range(21)]
    tasks = [PREFIX + f"task/c000/{i:032x}" for i in range(101)]
    data.update(
        {
            "list_clusters": {"clusterArns": clusters},
            "describe_clusters": lambda p: {
                "clusters": [{"clusterArn": arn} for arn in p["clusters"]]
            },
            "list_services": lambda p: {
                "serviceArns": services if p["cluster"] == clusters[0] else []
            },
            "describe_services": lambda p: {
                "services": [{"serviceArn": arn} for arn in p["services"]]
            },
            "list_tasks": lambda p: {"taskArns": tasks if p["cluster"] == clusters[0] else []},
            "describe_tasks": lambda p: {"tasks": [{"taskArn": arn} for arn in p["tasks"]]},
        }
    )
    result, session = collect(data)
    assert len(result.resources) == 223
    for method, key, sizes in [
        ("describe_clusters", "clusters", [100, 1]),
        ("describe_services", "services", [10, 10, 1]),
        ("describe_tasks", "tasks", [100, 1]),
    ]:
        assert [len(p[key]) for name, p in session.ecs.calls if name == method] == sizes


def test_deployment_revisions_are_read_and_a_failed_one_keeps_its_observed_arn():
    data = responses()
    other = DEFINITION.rsplit(":", 1)[0] + ":2"
    data["describe_services"]["services"][0]["deployments"] = [
        {"taskDefinition": DEFINITION},
        {"taskDefinition": other},
    ]
    primary = deepcopy(data["describe_task_definition"])
    data["describe_task_definition"] = lambda p: (
        primary if p["taskDefinition"] == DEFINITION else denied("DescribeTaskDefinition")
    )
    result, session = collect(data)
    definitions = {r.arn: r for r in result.resources if isinstance(r, EcsTaskDefinition)}
    assert definitions[DEFINITION].containers
    failed = definitions[other]
    assert failed.fact_evidence["/arn"] and failed.evidence_ids
    assert failed.containers is None
    assert sum(name == "describe_task_definition" for name, _ in session.ecs.calls) == 2
    assert coverage(result, Op.DESCRIBE_TASK_DEFINITION).status == "incomplete"
    assert "failure-private" not in json.dumps([c.model_dump(mode="json") for c in result.coverage])


def test_failure_array_keeps_successes_and_a_listed_skeleton_for_the_failed_resource():
    data = responses()
    other = PREFIX + "service/main/missing"
    data["list_services"]["serviceArns"].append(other)
    data["describe_services"]["failures"] = [
        {"arn": other, "reason": "private-reason", "detail": "private-detail"}
    ]
    result, _ = collect(data)
    resources = {r.arn: r for r in result.resources}
    assert resources[SERVICE].desired_count == 1
    skeleton = resources[other]
    assert skeleton.desired_count is None
    assert skeleton.cluster_arn == CLUSTER
    citation = result.evidence[skeleton.fact_evidence["/cluster_arn"][0]]
    assert citation.configuration_path == f'Request[{{"cluster":"{CLUSTER}"}}].cluster'
    assert citation.locator == "ecs:ListServices"
    services = coverage(result, Op.DESCRIBE_SERVICES)
    assert services.status == "incomplete"
    assert services.result_count == 1
    assert any(v.configuration_path.endswith(".failures[0]") for v in services.unresolved_values)
    assert coverage(result, Op.DESCRIBE_TASK_DEFINITION).status == "incomplete"
    assert "private-" not in json.dumps([c.model_dump(mode="json") for c in result.coverage])


def test_malformed_discovery_identity_is_not_empty_success():
    result, _ = collect({"list_clusters": {"clusterArns": ["not-an-arn"]}})
    assert result.resources == []
    assert coverage(result, Op.LIST_CLUSTERS).status == "incomplete"
    assert all(c.status == "skipped" for c in result.coverage if c.operation != Op.LIST_CLUSTERS)


def test_identity_must_use_the_resource_arn():
    identity = {"account_id": ACCOUNT, "region": REGION, "service": "ecs", "physical_id": SERVICE}
    with pytest.raises(ValidationError, match="ECS identity must use the resource ARN"):
        EcsService(identity=identity, arn=SERVICE + "other")
