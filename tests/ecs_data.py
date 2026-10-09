from copy import deepcopy
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.ecs import collect_ecs
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService

ACCOUNT = "123456789012"
REGION = AwsRegion.US_EAST_1
PREFIX = f"arn:aws:ecs:{REGION}:{ACCOUNT}:"
CLUSTER = PREFIX + "cluster/main"
SERVICE = PREFIX + "service/main/web"
TASK = PREFIX + "task/main/0123456789abcdef"
DEFINITION = PREFIX + "task-definition/web:1"
EXECUTION_ROLE = f"arn:aws:iam::{ACCOUNT}:role/execution"
TASK_ROLE = f"arn:aws:iam::{ACCOUNT}:role/web"
SNAPSHOT_ID = UUID(int=160)


def config(**overrides):
    return AwsAccountConfig(
        **{
            "profile": "test",
            "expected_account_id": ACCOUNT,
            "regions": (REGION,),
            "services": (AwsService.ECS,),
            "max_concurrency": 1,
            **overrides,
        }
    )


def responses():
    return {
        "list_clusters": {"clusterArns": [CLUSTER]},
        "describe_clusters": {
            "clusters": [
                {
                    "clusterArn": CLUSTER,
                    "clusterName": "main",
                    "status": "ACTIVE",
                    "runningTasksCount": 1,
                }
            ]
        },
        "list_services": {"serviceArns": [SERVICE]},
        "describe_services": {
            "services": [
                {
                    "serviceArn": SERVICE,
                    "serviceName": "web",
                    "clusterArn": CLUSTER,
                    "taskDefinition": DEFINITION,
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "launchType": "FARGATE",
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-a"],
                            "securityGroups": ["sg-a"],
                            "assignPublicIp": "DISABLED",
                        }
                    },
                }
            ],
            "failures": [],
        },
        "list_tasks": {"taskArns": [TASK]},
        "describe_tasks": {
            "tasks": [
                {
                    "taskArn": TASK,
                    "clusterArn": CLUSTER,
                    "taskDefinitionArn": DEFINITION,
                    "lastStatus": "RUNNING",
                    "desiredStatus": "RUNNING",
                    "cpu": "256",
                    "memory": "512",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [
                                {"name": "privateDnsName", "value": "discard-me"},
                                {"name": "subnetId", "value": "subnet-a"},
                                {"name": "networkInterfaceId", "value": "eni-a"},
                            ],
                        }
                    ],
                }
            ],
            "failures": [],
        },
        "describe_task_definition": {
            "taskDefinition": {
                "taskDefinitionArn": DEFINITION,
                "family": "web",
                "revision": 1,
                "executionRoleArn": EXECUTION_ROLE,
                "taskRoleArn": TASK_ROLE,
                "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
                "cpu": "256",
                "memory": "512",
                "containerDefinitions": [
                    {
                        "name": "web",
                        "image": "registry.example/web:1",
                        "cpu": 256,
                        "essential": True,
                        "portMappings": [
                            {"containerPort": 8080, "hostPort": 8080, "protocol": "tcp"}
                        ],
                    }
                ],
            }
        },
    }


class Client:
    def __init__(self, values=None):
        self.values = responses() if values is None else values
        self.calls = []

    def __getattr__(self, method):
        def call(**params):
            self.calls.append((method, deepcopy(params)))
            value = self.values[method]
            if callable(value):
                value = value(params)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        return call


class Session:
    def __init__(self, values=None):
        self.ecs = Client(values)
        self.clients = []

    def client(self, name, **kwargs):
        assert name == "ecs"
        self.clients.append((name, kwargs))
        return self.ecs


def collect(values=None):
    session = Session(values)
    result = collect_ecs(session, config(), AdapterContext(), SNAPSHOT_ID, ACCOUNT, REGION)
    return result, session
