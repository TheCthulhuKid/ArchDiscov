import json
from copy import deepcopy

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.ecs import EcsService, EcsTask, EcsTaskDefinition
from archdiscov.services import load_snapshot, save_snapshot

from .ecs_data import (
    ACCOUNT,
    CLUSTER,
    DEFINITION,
    EXECUTION_ROLE,
    REGION,
    SERVICE,
    SNAPSHOT_ID,
    TASK_ROLE,
    collect,
    responses,
)


def snapshot(result):
    return ArchitectureSnapshot(
        schema_version="1",
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT,
        collection_started_at=min(c.started_at for c in result.coverage),
        collection_completed_at=max(c.completed_at for c in result.coverage),
        source={"kind": "aws", "profile": "test"},
        coverage={"regions": [REGION], "account_wide": False, "operations": result.coverage},
        regions={REGION: {"resources": result.resources}},
        evidence=result.evidence,
    )


def fact(result, resource, pointer):
    return [result.evidence[eid] for eid in resource.fact_evidence[pointer]]


def test_exact_facts_include_request_page_and_source_field_context():
    result, _ = collect()
    service = next(r for r in result.resources if isinstance(r, EcsService))
    task = next(r for r in result.resources if isinstance(r, EcsTask))
    definition = next(r for r in result.resources if isinstance(r, EcsTaskDefinition))
    assert fact(result, service, "/task_definition_arn")[0].value == DEFINITION
    network = fact(result, service, "/subnet_ids")[0]
    assert network.value == ["subnet-a"]
    assert network.configuration_path == (
        f'Request[{{"cluster":"{CLUSTER}","services":["{SERVICE}"]}}]'
        ".Page[0].services[0].networkConfiguration.awsvpcConfiguration.subnets"
    )
    assert network.locator == "ecs:DescribeServices"
    assert fact(result, service, "/security_group_ids")[0].value == ["sg-a"]
    eni = fact(result, task, "/network_interface_ids")[0]
    assert eni.configuration_path.endswith(".Page[0].tasks[0].attachments[0].details[2].value")
    assert eni.value == "eni-a"
    assert fact(result, task, "/subnet_ids")[0].configuration_path.endswith("details[1].value")
    assert fact(result, definition, "/execution_role_arn")[0].value == EXECUTION_ROLE
    assert fact(result, definition, "/task_role_arn")[0].value == TASK_ROLE
    assert fact(result, definition, "/containers/0/image")[0].value == "registry.example/web:1"
    port = fact(result, definition, "/containers/0/port_mappings/0/container_port")[0]
    assert port.value == 8080
    assert port.configuration_path.endswith(
        ".containerDefinitions[0].portMappings[0].containerPort"
    )
    snapshot(result)  # Validate ownership and every normalized fact pointer.


def test_skeleton_cluster_citation_uses_list_request_scope():
    data = responses()
    data["describe_services"] = {"services": []}
    result, _ = collect(data)
    service = next(r for r in result.resources if isinstance(r, EcsService))
    citation = fact(result, service, "/cluster_arn")[0]
    assert citation.configuration_path == f'Request[{{"cluster":"{CLUSTER}"}}].cluster'
    assert citation.value == CLUSTER
    assert citation.locator == "ecs:ListServices"
    snapshot(result)


def test_source_selection_excludes_all_workload_content_before_evidence_and_save(tmp_path):
    data = responses()
    service = data["describe_services"]["services"][0]
    task = data["describe_tasks"]["tasks"][0]
    definition = data["describe_task_definition"]["taskDefinition"]
    container = definition["containerDefinitions"][0]
    secret_fields = {
        "environment": [{"name": "HARMLESS", "value": "private-env-value"}],
        "environmentFiles": [{"value": "private-env-file", "type": "s3"}],
        "command": ["private-command"],
        "entryPoint": ["private-entrypoint"],
        "secrets": [{"name": "HARMLESS", "valueFrom": "private-secret-reference"}],
        "logConfiguration": {
            "logDriver": "splunk",
            "secretOptions": [
                {"name": "HARMLESS", "valueFrom": "private-log-secret"},
            ],
            "options": {"harmless": "private-log-option"},
        },
        "healthCheck": {"command": ["private-health-command"]},
        "repositoryCredentials": {"credentialsParameter": "private-repository-secret"},
        "description": "private-description",
        "unknownNewField": {"value": "private-new-field"},
        "dockerLabels": {"harmless": "private-docker-label"},
    }
    container.update(deepcopy(secret_fields))
    definition.update(
        {
            "description": "private-definition-description",
            "tags": [{"key": "harmless", "value": "private-tag"}],
        }
    )
    task.update(
        {
            "overrides": {"containerOverrides": [deepcopy(secret_fields)]},
            "stoppedReason": "private-stopped-reason",
        }
    )
    service.update(
        {
            "events": [{"message": "private-event-message"}],
            "description": "private-service-description",
        }
    )
    original = deepcopy(data)
    result, _ = collect(data)
    assert data == original
    encoded = json.dumps(
        {
            "resources": [r.model_dump(mode="json") for r in result.resources],
            "evidence": {
                key: value.model_dump(mode="json") for key, value in result.evidence.items()
            },
            "coverage": [item.model_dump(mode="json") for item in result.coverage],
        }
    )
    assert "private-" not in encoded
    assert "discard-me" not in encoded
    for key in secret_fields:
        assert f'"{key}"' not in encoded
    snap = snapshot(result)
    before = snap.model_dump(mode="json")
    path = tmp_path / "ecs.json"
    save_snapshot(snap, path)
    loaded = load_snapshot(path)
    assert "private-" not in path.read_text()
    assert loaded.model_dump(mode="json") == before
    assert snap.model_dump(mode="json") == before
    assert data == original
    assert (
        next(r for r in loaded.regions[REGION].resources if isinstance(r, EcsTaskDefinition))
        .containers[0]
        .image
        == "registry.example/web:1"
    )
