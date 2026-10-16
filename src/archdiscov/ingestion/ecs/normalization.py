"""Allowlist ECS source selection before creating any evidence."""

from uuid import UUID

from ...models.enums import AwsRegion, AwsService
from ...models.resource_base import Resource
from ...models.resources.ecs import EcsCluster, EcsService, EcsTask, EcsTaskDefinition
from ..detail_execution import DetailResponse
from ..evidence_builder import EvidenceBuilder
from ..operations import EcsOperation as Op
from .discovery import EcsInventory, valid_arn

_CLUSTER = {
    "arn": "clusterArn",
    "name": "clusterName",
    "status": "status",
    "capacity_providers": "capacityProviders",
    "running_tasks_count": "runningTasksCount",
    "pending_tasks_count": "pendingTasksCount",
    "active_services_count": "activeServicesCount",
}
_SERVICE = {
    "arn": "serviceArn",
    "name": "serviceName",
    "cluster_arn": "clusterArn",
    "task_definition_arn": "taskDefinition",
    "status": "status",
    "desired_count": "desiredCount",
    "running_count": "runningCount",
    "pending_count": "pendingCount",
    "launch_type": "launchType",
    "scheduling_strategy": "schedulingStrategy",
    "platform_version": "platformVersion",
}
_TASK = {
    "arn": "taskArn",
    "cluster_arn": "clusterArn",
    "task_definition_arn": "taskDefinitionArn",
    "desired_status": "desiredStatus",
    "last_status": "lastStatus",
    "launch_type": "launchType",
    "platform_version": "platformVersion",
    "availability_zone": "availabilityZone",
    "cpu": "cpu",
    "memory": "memory",
}
_DEFINITION = {
    "arn": "taskDefinitionArn",
    "execution_role_arn": "executionRoleArn",
    "task_role_arn": "taskRoleArn",
    "family": "family",
    "revision": "revision",
    "status": "status",
    "network_mode": "networkMode",
    "cpu": "cpu",
    "memory": "memory",
    "requires_compatibilities": "requiresCompatibilities",
}
_CONTAINER = {
    "name": "name",
    "image": "image",
    "cpu": "cpu",
    "memory": "memory",
    "memory_reservation": "memoryReservation",
    "essential": "essential",
}
_PORT = {
    "container_port": "containerPort",
    "host_port": "hostPort",
    "protocol": "protocol",
    "app_protocol": "appProtocol",
    "container_port_range": "containerPortRange",
}
_NETWORK = {
    "subnet_ids": "subnets",
    "security_group_ids": "securityGroups",
    "assign_public_ip": "assignPublicIp",
}


def _select(item: dict, mapping: dict[str, str]) -> dict:
    # Only schema-selected configuration is eligible for evidence, not arbitrary children.
    return {source: item[source] for source in mapping.values() if source in item}


def _selected(item: dict, mapping: dict[str, str]) -> dict:
    safe = _select(item, mapping)
    if mapping is _SERVICE:
        network = item.get("networkConfiguration", {}).get("awsvpcConfiguration")
        if isinstance(network, dict):
            safe["networkConfiguration"] = {"awsvpcConfiguration": _select(network, _NETWORK)}
        if isinstance(item.get("deployments"), list):
            safe["deployments"] = [
                _select(value, {"arn": "taskDefinition"}) if isinstance(value, dict) else {}
                for value in item["deployments"]
            ]
    if mapping is _TASK and isinstance(item.get("attachments"), list):
        safe["attachments"] = []
        for attachment in item["attachments"]:
            selected = {}
            if isinstance(attachment, dict) and attachment.get("type") == "ElasticNetworkInterface":
                selected["type"] = attachment["type"]
                if isinstance(attachment.get("details"), list):
                    selected["details"] = [
                        {"name": detail["name"], "value": detail.get("value")}
                        if isinstance(detail, dict)
                        and detail.get("name")
                        in {"subnetId", "networkInterfaceId", "securityGroupId"}
                        else {}
                        for detail in attachment["details"]
                    ]
            safe["attachments"].append(selected)
    if mapping is _DEFINITION and isinstance(item.get("containerDefinitions"), list):
        safe["containerDefinitions"] = []
        for container in item["containerDefinitions"]:
            selected = _select(container, _CONTAINER) if isinstance(container, dict) else {}
            if isinstance(container, dict) and isinstance(container.get("portMappings"), list):
                selected["portMappings"] = [
                    _select(port, _PORT) if isinstance(port, dict) else {}
                    for port in container["portMappings"]
                ]
            safe["containerDefinitions"].append(selected)
    return safe


def _mapped(item: dict, mapping: dict[str, str]) -> dict:
    return {field: item[source] for field, source in mapping.items() if source in item}


class EcsBuilder(EvidenceBuilder):
    def build(self, cls, mapping: dict, response: DetailResponse) -> Resource:
        item = _selected(response.value, mapping)
        fields = _mapped(item, mapping)
        facts = {
            f"/{field}": (source, fields[field])
            for field, source in mapping.items()
            if field in fields
        }
        if mapping is _SERVICE and "networkConfiguration" in item:
            network = item["networkConfiguration"]["awsvpcConfiguration"]
            fields.update(_mapped(network, _NETWORK))
            facts.update(
                {
                    f"/{field}": (
                        f"networkConfiguration.awsvpcConfiguration.{source}",
                        fields[field],
                    )
                    for field, source in _NETWORK.items()
                    if field in fields
                }
            )
        if mapping is _DEFINITION and "containerDefinitions" in item:
            containers = []
            for index, container in enumerate(item["containerDefinitions"]):
                mapped = _mapped(container, _CONTAINER)
                facts.update(
                    {
                        f"/containers/{index}/{field}": (
                            f"containerDefinitions[{index}].{source}",
                            mapped[field],
                        )
                        for field, source in _CONTAINER.items()
                        if field in mapped
                    }
                )
                if "portMappings" in container:
                    mapped["port_mappings"] = [
                        _mapped(port, _PORT) for port in container["portMappings"]
                    ]
                    for port_index, port in enumerate(mapped["port_mappings"]):
                        facts.update(
                            {
                                f"/containers/{index}/port_mappings/{port_index}/{field}": (
                                    f"containerDefinitions[{index}].portMappings[{port_index}].{source}",
                                    port[field],
                                )
                                for field, source in _PORT.items()
                                if field in port
                            }
                        )
                containers.append(mapped)
            fields["containers"] = containers
            facts["/containers"] = ("containerDefinitions", item["containerDefinitions"])
        resource = self._build(
            cls,
            operation=response.call.operation,
            item_path=response.path,
            item=item,
            physical_id=fields["arn"],
            attributes={},
            facts=facts,
            **fields,
        )
        if mapping is _TASK:
            for index, attachment in enumerate(item.get("attachments", [])):
                for detail_index, detail in enumerate(attachment.get("details", [])):
                    field = {
                        "subnetId": "subnet_ids",
                        "networkInterfaceId": "network_interface_ids",
                        "securityGroupId": "security_group_ids",
                    }.get(detail.get("name"))
                    value = detail.get("value")
                    if field and isinstance(value, str) and value.strip():
                        values = getattr(resource, field)
                        if values is None:
                            values = []
                            setattr(resource, field, values)
                        if value not in values:
                            values.append(value)
                        path = f"{response.path}.attachments[{index}].details[{detail_index}].value"
                        self.cite(resource, f"/{field}", response.call.operation, path, value)
                        self.cite(
                            resource,
                            f"/{field}/{values.index(value)}",
                            response.call.operation,
                            path,
                            value,
                        )
        return resource


def normalize(inventory: EcsInventory, snapshot_id: UUID, account_id: str, region: AwsRegion):
    builder = EcsBuilder(snapshot_id, account_id, region, AwsService.ECS)
    resources: dict[str, Resource] = {}
    stages = [
        (Op.LIST_CLUSTERS, Op.DESCRIBE_CLUSTERS, EcsCluster, _CLUSTER),
        (Op.LIST_SERVICES, Op.DESCRIBE_SERVICES, EcsService, _SERVICE),
        (Op.LIST_TASKS, Op.DESCRIBE_TASKS, EcsTask, _TASK),
    ]
    for listing, describing, cls, mapping in stages:
        for response in inventory.results[describing].responses:
            resource = builder.build(cls, mapping, response)
            previous = resources.get(resource.arn)
            if previous is not None:
                resource.evidence_ids.extend(previous.evidence_ids)
                for pointer, ids in previous.fact_evidence.items():
                    resource.fact_evidence.setdefault(pointer, []).extend(ids)
            resources[resource.arn] = resource
        for response in inventory.results[listing].responses:
            arn = response.value
            if arn not in resources:
                # ARN was observed, not guessed; details remain explicitly unknown.
                identity = dict(
                    account_id=account_id, region=region, service=AwsService.ECS, physical_id=arn
                )
                resources[arn] = cls(identity=identity, arn=arn)
            resource = resources[arn]
            builder.cite(resource, "/arn", listing, response.path, arn)
            source_id = builder.source_evidence(resource, listing, response.path, arn)
            resource.evidence_ids.append(source_id)
            if listing != Op.LIST_CLUSTERS and resource.cluster_arn is None:
                resource.cluster_arn = response.call.params["cluster"]
                request_path = response.path.split(".Page[", 1)[0] + ".cluster"
                builder.cite(resource, "/cluster_arn", listing, request_path, resource.cluster_arn)
    # Definitions referenced by services, tasks, and service deployments survive failed details.
    definitions = inventory.results[Op.DESCRIBE_TASK_DEFINITION]
    for response in definitions.responses:
        resource = builder.build(EcsTaskDefinition, _DEFINITION, response)
        resources[resource.arn] = resource
    for op, source_key in [
        (Op.DESCRIBE_SERVICES, "taskDefinition"),
        (Op.DESCRIBE_TASKS, "taskDefinitionArn"),
    ]:
        for response in inventory.results[op].responses:
            references = [(source_key, response.value.get(source_key))]
            if op == Op.DESCRIBE_SERVICES:
                deployments = response.value.get("deployments", [])
                if isinstance(deployments, list):
                    references.extend(
                        (f"deployments[{index}].taskDefinition", item.get("taskDefinition"))
                        for index, item in enumerate(deployments)
                        if isinstance(item, dict)
                    )
            for path, arn in references:
                if not valid_arn(arn, "task-definition", account_id, region):
                    continue
                if arn not in resources:
                    resources[arn] = EcsTaskDefinition(
                        identity=dict(
                            account_id=account_id,
                            region=region,
                            service=AwsService.ECS,
                            physical_id=arn,
                        ),
                        arn=arn,
                    )
                resource = resources[arn]
                builder.cite(resource, "/arn", op, f"{response.path}.{path}", arn)
                evidence_id = builder.source_evidence(resource, op, f"{response.path}.{path}", arn)
                resource.evidence_ids.append(evidence_id)
    return list(resources.values()), builder.evidence
