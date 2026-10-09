import json
from copy import deepcopy

import pytest
from botocore.exceptions import ClientError

from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.operations import EcsOperation as Op
from archdiscov.models.ecs import EcsCluster, EcsService, EcsTask, EcsTaskDefinition

from .ecs_data import CLUSTER, DEFINITION, PREFIX, SERVICE, TASK, collect, responses


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


@pytest.mark.parametrize(
    "method,key",
    [
        ("list_clusters", "clusterArns"),
        ("list_services", "serviceArns"),
        ("list_tasks", "taskArns"),
    ],
)
def test_next_token_paging_retains_duplicate_observations_deduplicates_detail_calls(method, key):
    data = responses()
    page = data[method]
    data[method] = lambda params: {
        **page,
        **({"nextToken": "page-two"} if "nextToken" not in params else {}),
    }
    result, session = collect(data)
    calls = [params for name, params in session.ecs.calls if name == method]
    assert len(calls) == 2
    assert calls[1]["nextToken"] == "page-two"
    assert len(result.resources) == 4
    assert all(item.status == "success" for item in result.coverage)
    source = next(r for r in result.resources if r.arn == page[key][0])
    assert any(
        ".Page[1]." in result.evidence[eid].configuration_path
        for eid in source.fact_evidence["/arn"]
    )
    assert all(
        sum(name == detail for name, _ in session.ecs.calls) == 1
        for detail in ("describe_clusters", "describe_services", "describe_tasks")
    )


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


@pytest.mark.parametrize(
    "method,arn",
    [
        ("describe_clusters", CLUSTER),
        ("describe_services", SERVICE),
        ("describe_tasks", TASK),
        ("describe_task_definition", DEFINITION),
    ],
)
def test_failed_details_retain_observed_arn_with_citations(method, arn):
    data = responses()
    data[method] = denied(method)
    result, _ = collect(data)
    resource = next(r for r in result.resources if r.arn == arn)
    assert resource.fact_evidence["/arn"]
    assert resource.evidence_ids
    operation = {
        "describe_clusters": Op.DESCRIBE_CLUSTERS,
        "describe_services": Op.DESCRIBE_SERVICES,
        "describe_tasks": Op.DESCRIBE_TASKS,
        "describe_task_definition": Op.DESCRIBE_TASK_DEFINITION,
    }[method]
    assert coverage(result, operation).status == "failed"
    assert "failure-private" not in json.dumps([c.model_dump(mode="json") for c in result.coverage])


@pytest.mark.parametrize(
    "method,key,arn,op",
    [
        ("describe_clusters", "clusters", CLUSTER, Op.DESCRIBE_CLUSTERS),
        ("describe_services", "services", SERVICE, Op.DESCRIBE_SERVICES),
        ("describe_tasks", "tasks", TASK, Op.DESCRIBE_TASKS),
    ],
)
def test_failure_arrays_qualify_coverage_preserve_successes(method, key, arn, op):
    data = responses()
    data[method]["failures"] = [
        {"arn": arn, "reason": "private-reason", "detail": "private-detail"}
    ]
    result, _ = collect(data)
    resource = next(r for r in result.resources if r.arn == arn)
    assert len(resource.fact_evidence) > 1
    assert coverage(result, op).status == "incomplete"
    assert coverage(result, op).result_count == 1
    assert coverage(result, op).unresolved_values[0].configuration_path.endswith(".failures[0]")
    assert "private-" not in json.dumps([c.model_dump(mode="json") for c in result.coverage])


def test_partial_detail_array_retains_skeleton_and_success():
    data = responses()
    other = PREFIX + "service/main/missing"
    data["list_services"]["serviceArns"].append(other)
    data["describe_services"]["failures"] = [{"arn": other, "reason": "MISSING"}]
    result, _ = collect(data)
    resources = {r.arn: r for r in result.resources}
    assert resources[SERVICE].desired_count == 1
    assert resources[other].desired_count is None
    assert resources[other].cluster_arn == CLUSTER
    assert coverage(result, Op.DESCRIBE_SERVICES).status == "incomplete"
    assert coverage(result, Op.DESCRIBE_TASK_DEFINITION).status == "incomplete"


def test_genuine_zero_is_success_without_fabricated_resources():
    result, session = collect({"list_clusters": {"clusterArns": []}})
    assert result.resources == []
    assert result.evidence == {}
    assert all(c.status == "success" and c.result_count == 0 for c in result.coverage)
    assert [method for method, _ in session.ecs.calls] == ["list_clusters"]


@pytest.mark.parametrize(
    "page", [{}, {"clusterArns": None}, {"clusterArns": ["not-an-arn"]}, denied("ListClusters")]
)
def test_unavailable_discovery_is_not_empty_success(page):
    result, _ = collect({"list_clusters": page})
    assert result.resources == []
    assert coverage(result, Op.LIST_CLUSTERS).status in {"failed", "incomplete"}
    assert all(c.status == "skipped" for c in result.coverage if c.operation != Op.LIST_CLUSTERS)


def test_later_page_failure_preserves_discovery_and_qualifies_downstream():
    data = responses()

    def page(params):
        if "nextToken" in params:
            raise denied("ListServices")
        return {"serviceArns": [SERVICE], "nextToken": "next"}

    data["list_services"] = page
    result, _ = collect(data)
    assert next(r for r in result.resources if r.arn == SERVICE).desired_count == 1
    assert coverage(result, Op.LIST_SERVICES).status == "incomplete"
    assert coverage(result, Op.DESCRIBE_SERVICES).status == "incomplete"
    assert coverage(result, Op.DESCRIBE_TASKS).status == "success"


def test_missing_detail_is_incomplete_not_zero_success():
    data = responses()
    data["describe_tasks"] = {"tasks": []}
    result, _ = collect(data)
    assert coverage(result, Op.DESCRIBE_TASKS).status == "incomplete"
    assert next(r for r in result.resources if r.arn == TASK).last_status is None


def test_deployment_definition_revisions_are_deduplicated_and_retained_after_failure():
    data = responses()
    other = DEFINITION.rsplit(":", 1)[0] + ":2"
    data["describe_services"]["services"][0]["deployments"] = [
        {"taskDefinition": DEFINITION},
        {"taskDefinition": other},
        {"taskDefinition": other},
    ]
    primary = deepcopy(data["describe_task_definition"])
    data["describe_task_definition"] = lambda p: (
        primary if p["taskDefinition"] == DEFINITION else denied("DescribeTaskDefinition")
    )
    result, session = collect(data)
    assert len(result.resources) == 5
    assert next(r for r in result.resources if r.arn == other).containers is None
    assert sum(name == "describe_task_definition" for name, _ in session.ecs.calls) == 2
    assert coverage(result, Op.DESCRIBE_TASK_DEFINITION).status == "incomplete"
