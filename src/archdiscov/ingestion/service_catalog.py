"""Static supported-service metadata; collectors remain ordinary explicit adapters."""

from dataclasses import dataclass
from types import MappingProxyType

from ..models.enums import AwsService
from .operations import (
    Ec2Operation,
    EcsOperation,
    Elbv2Operation,
    IamOperation,
    LambdaOperation,
    RdsOperation,
    SqsOperation,
)


@dataclass(frozen=True)
class ServiceDefinition:
    client_name: str
    operations: tuple[str, ...]
    account_wide: bool = False
    configuration_collector: tuple[str, str] | None = None


SERVICE_CATALOG = MappingProxyType(
    {
        AwsService.EC2: ServiceDefinition(
            "ec2", tuple(op for op in Ec2Operation if op is not Ec2Operation.DESCRIBE_REGIONS)
        ),
        AwsService.ELASTIC_LOAD_BALANCING: ServiceDefinition("elbv2", tuple(Elbv2Operation)),
        AwsService.IAM: ServiceDefinition("iam", tuple(IamOperation), account_wide=True),
        AwsService.ECS: ServiceDefinition(
            "ecs", tuple(EcsOperation), configuration_collector=("ecs", "collect_ecs")
        ),
        AwsService.LAMBDA: ServiceDefinition(
            "lambda",
            tuple(LambdaOperation),
            configuration_collector=("functions", "collect_lambda"),
        ),
        AwsService.RDS: ServiceDefinition(
            "rds", tuple(RdsOperation), configuration_collector=("database", "collect_rds")
        ),
        AwsService.SQS: ServiceDefinition(
            "sqs", tuple(SqsOperation), configuration_collector=("queues", "collect_sqs")
        ),
    }
)
COLLECTABLE_SERVICES = frozenset(SERVICE_CATALOG)
ACCOUNT_WIDE_SERVICES = frozenset(
    service for service, definition in SERVICE_CATALOG.items() if definition.account_wide
)
CONFIGURATION_SERVICES = tuple(
    service
    for service, definition in SERVICE_CATALOG.items()
    if definition.configuration_collector is not None
)
REGIONAL_OPERATIONS = MappingProxyType(
    {
        service: definition.operations
        for service, definition in SERVICE_CATALOG.items()
        if not definition.account_wide
    }
)


def client_name(service: AwsService) -> str:
    definition = SERVICE_CATALOG.get(service)
    return definition.client_name if definition is not None else service.value
