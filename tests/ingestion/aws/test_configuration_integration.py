from typing import ClassVar

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.operations import (
    EcsOperation,
    LambdaOperation,
    RdsOperation,
    SqsOperation,
)
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.models.enums import AwsService
from archdiscov.services import collect_and_save, collect_snapshots, load_snapshot

SERVICES = (AwsService.ECS, AwsService.LAMBDA, AwsService.RDS, AwsService.SQS)


class EmptyClient:
    replies: ClassVar[dict[str, dict]] = {
        "list_clusters": {"clusterArns": []},
        "list_functions": {"Functions": []},
        "describe_db_instances": {"DBInstances": []},
        "describe_db_clusters": {"DBClusters": []},
        "describe_db_subnet_groups": {"DBSubnetGroups": []},
        # SQS omits QueueUrls when the account has no queues.
        "list_queues": {},
    }

    def __getattr__(self, method):
        def read(**params):
            assert method in self.replies, f"unexpected AWS operation: {method}"
            return self.replies[method]

        return read


class Session:
    def __init__(self):
        self.empty = EmptyClient()

    def client(self, name, **kwargs):
        if name == "sts":
            return self
        return self.empty

    def get_caller_identity(self):
        return {"Account": "123456789012"}


def request(services=SERVICES, regions=("us-east-1",)):
    return AwsCollectionRequest(
        accounts=(
            AwsAccountConfig(
                profile="dev",
                expected_account_id="123456789012",
                regions=regions,
                services=services,
            ),
        )
    )


def test_combined_empty_inventory_is_complete_per_service_and_region():
    session = Session()
    (snapshot,) = collect_snapshots(
        request(regions=("us-east-1", "us-west-2")),
        context=AdapterContext(session_factory=lambda **kwargs: session),
    )
    operations = [*EcsOperation, *LambdaOperation, *RdsOperation, *SqsOperation]
    assert len(snapshot.coverage.operations) == len(operations) * 2
    assert all(
        op.status == "success" and op.result_count == 0 for op in snapshot.coverage.operations
    )
    assert {op.service for op in snapshot.coverage.operations} == set(SERVICES)
    assert {op.region for op in snapshot.coverage.operations} == {"us-east-1", "us-west-2"}
    assert snapshot.schema_version == "2"
    assert all(not region.resources for region in snapshot.regions.values())


def test_nonempty_combined_services_persist_source_owned_links(tmp_path):
    from archdiscov.ingestion.configuration_relationships import walk_resources
    from archdiscov.models.resources.database import DbInstance
    from archdiscov.models.resources.ecs import EcsService
    from archdiscov.models.resources.functions import LambdaFunction
    from archdiscov.models.resources.queues import SqsQueue
    from tests.support.aws_fakes import Session as Ec2Session
    from tests.support.ecs_data import Client as EcsClient
    from tests.support.elbv2_data import ec2_pages
    from tests.support.lambda_data import Client as LambdaClient
    from tests.support.rds_data import Client as RdsClient
    from tests.support.rds_data import data as rds_data
    from tests.support.sqs_data import Client as SqsClient

    class CombinedSession(Session):
        def __init__(self):
            super().__init__()
            self.ec2 = Ec2Session(pages=ec2_pages)
            self.services = {
                "ecs": EcsClient(),
                "lambda": LambdaClient(),
                "rds": RdsClient(rds_data()),
                "sqs": SqsClient(),
            }

        def client(self, name, **kwargs):
            if name == "sts":
                return self
            if name == "ec2":
                return self.ec2.client(name, **kwargs)
            return self.services[name]

    session = CombinedSession()
    context = AdapterContext(session_factory=lambda **kwargs: session)
    output = tmp_path / "combined.json"
    outcome = collect_and_save(request((AwsService.EC2, *SERVICES)), [output], context=context)[0]
    assert outcome.error is None
    snapshot = load_snapshot(output)
    resources = list(walk_resources(snapshot.regions["us-east-1"].resources))
    assert all(
        any(isinstance(r, cls) for r in resources)
        for cls in (EcsService, LambdaFunction, DbInstance, SqsQueue)
    )
    links = snapshot.dependencies_by_region["us-east-1"]
    kinds = {link.attributes.get("kind") for link in links if link.target.kind == "local"}
    assert {
        "cluster_membership",
        "task_definition",
        "database_cluster",
        "database_subnet_group",
        "security_group_membership",
        "subnet_membership",
    } <= kinds
    assert any(
        link.attributes.get("kind") == "dead_letter_queue" and link.target.kind == "unresolved"
        for link in links
    )
    assert any(
        link.attributes.get("kind") == "execution_role" and link.target.kind == "unresolved"
        for link in links
    )
    assert any(
        link.attributes.get("kind") == "resource_policy_principal"
        and link.attributes.get("principal_kind") == "service"
        and link.target.kind == "external"
        for link in links
    )
    for link in links:
        assert all(
            snapshot.evidence[eid].resource_id == link.source_id for eid in link.evidence_ids
        )
    assert "secret-" not in output.read_text()
