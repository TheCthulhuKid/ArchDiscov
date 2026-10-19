import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from archdiscov import cli
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
    replies = {
        "list_clusters": {"clusterArns": []},
        "list_functions": {"Functions": []},
        "describe_db_instances": {"DBInstances": []},
        "describe_db_clusters": {"DBClusters": []},
        "describe_db_subnet_groups": {"DBSubnetGroups": []},
        "list_queues": {"QueueUrls": []},
    }

    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        def read(**params):
            self.calls.append((method, params))
            assert method in self.replies, f"unexpected AWS operation: {method}"
            return self.replies[method]

        return read


class Session:
    def __init__(self):
        self.clients = []
        self.empty = EmptyClient()

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
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


def test_combined_empty_inventory_is_complete_per_service_and_region(tmp_path):
    session = Session()
    outcomes = collect_and_save(
        request(regions=("us-east-1", "us-west-2")),
        [tmp_path / "empty.json"],
        context=AdapterContext(session_factory=lambda **kwargs: session),
    )
    assert outcomes[0].error is None
    assert outcomes[0].complete
    snapshot = load_snapshot(outcomes[0].path)
    operations = [*EcsOperation, *LambdaOperation, *RdsOperation, *SqsOperation]
    assert len(snapshot.coverage.operations) == len(operations) * 2
    assert all(
        op.status == "success" and op.result_count == 0 for op in snapshot.coverage.operations
    )
    assert {op.service for op in snapshot.coverage.operations} == set(SERVICES)
    assert {op.region for op in snapshot.coverage.operations} == {"us-east-1", "us-west-2"}
    assert snapshot.schema_version == "2"
    assert all(not region.resources for region in snapshot.regions.values())
    before = outcomes[0].path.read_bytes()
    conflict = collect_and_save(
        request(),
        [outcomes[0].path],
        context=AdapterContext(session_factory=lambda **kwargs: session),
    )[0]
    assert conflict.error.code == "snapshot_conflict"
    assert outcomes[0].path.read_bytes() == before


@pytest.mark.parametrize("service", SERVICES)
def test_collect_cli_accepts_each_regional_service(monkeypatch, tmp_path, service):
    session = Session()
    real_collect = cli.collect_and_save
    monkeypatch.setattr(
        cli,
        "collect_and_save",
        lambda req, paths: real_collect(
            req, paths, context=AdapterContext(session_factory=lambda **kwargs: session)
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "collect",
            "--account",
            "dev:123456789012",
            "--region",
            "us-east-1",
            "--service",
            service.value,
            "--output",
            str(tmp_path / "snapshot.json"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["accounts"][0]["complete"]
    assert {op["service"] for op in payload["accounts"][0]["operations"]} == {service.value}
    assert all(name in {service.value, "sts"} for name, _ in session.clients)
    missing_region = CliRunner().invoke(
        cli.app,
        [
            "collect",
            "--account",
            "dev:123456789012",
            "--service",
            service.value,
            "--output",
            str(tmp_path / "bad.json"),
            "--json",
        ],
    )
    assert missing_region.exit_code == 2
    assert not (tmp_path / "bad.json").exists()


def test_installed_help_names_all_services_outside_checkout(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("AWS_")
    }
    result = subprocess.run(
        [sys.executable, "-m", "archdiscov", "collect", "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert all(service.value in result.stdout for service in SERVICES)


def test_identity_failure_prevents_new_service_calls():
    class WrongAccount(Session):
        def get_caller_identity(self):
            return {"Account": "999999999999"}

    session = WrongAccount()
    from archdiscov.ingestion.adapter import IngestionError

    with pytest.raises(IngestionError, match="does not match"):
        collect_snapshots(
            request(), context=AdapterContext(session_factory=lambda **kwargs: session)
        )
    assert session.empty.calls == []


def test_nonempty_combined_services_persist_links_and_inspect_fact_evidence(monkeypatch, tmp_path):
    from archdiscov.ingestion.configuration_relationships import walk_resources
    from archdiscov.models.resources.database import DbInstance
    from archdiscov.models.resources.ecs import EcsService
    from archdiscov.models.resources.functions import LambdaFunction
    from archdiscov.models.resources.queues import SqsQueue
    from tests.support.aws_fakes import Session as Ec2Session
    from tests.support.ecs_data import Client as EcsClient

    from .test_elbv2_collection import ec2_pages
    from .test_lambda_collection import Client as LambdaClient
    from .test_rds_collection import Client as RdsClient
    from .test_rds_collection import data as rds_data
    from .test_sqs_collection import Client as SqsClient

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
            self.clients.append((name, kwargs))
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
    database = next(r for r in resources if isinstance(r, DbInstance))
    evidence_id = database.fact_evidence["/endpoint_port"][0]
    inspected = CliRunner().invoke(
        cli.app, ["evidence", evidence_id, "--snapshot", str(output), "--json"]
    )
    assert inspected.exit_code == 0, inspected.output
    payload = json.loads(inspected.output)
    assert payload["evidence"]["value"] == 5432
    assert payload["evidence"]["configuration_path"].endswith("Endpoint.Port")
    assert snapshot.model_dump(mode="json") == outcome.snapshot.model_dump(mode="json")


def test_new_service_partial_failure_keeps_other_service_results(tmp_path):
    from botocore.exceptions import ClientError

    from .test_lambda_collection import Client as LambdaClient
    from .test_rds_collection import Client as RdsClient
    from .test_rds_collection import data as rds_data

    class PartialSession(Session):
        def __init__(self):
            super().__init__()
            self.function = LambdaClient(
                detail=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "injected-private-value"}},
                    "GetFunctionConfiguration",
                )
            )
            self.database = RdsClient(rds_data())

        def client(self, name, **kwargs):
            if name == "sts":
                return self
            return self.function if name == "lambda" else self.database

    session = PartialSession()
    outcome = collect_and_save(
        request((AwsService.LAMBDA, AwsService.RDS)),
        [tmp_path / "partial.json"],
        context=AdapterContext(session_factory=lambda **kwargs: session),
    )[0]
    assert outcome.error is None
    assert not outcome.complete
    assert len(outcome.snapshot.regions["us-east-1"].resources) == 4
    operations = {op.operation: op for op in outcome.snapshot.coverage.operations}
    assert operations["GetFunctionConfiguration"].status == "failed"
    assert operations["DescribeDBInstances"].status == "success"
    assert "injected-private-value" not in outcome.path.read_text()
