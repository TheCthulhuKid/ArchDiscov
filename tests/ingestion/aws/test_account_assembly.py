from copy import deepcopy
from datetime import UTC, datetime
from random import Random
from threading import Lock
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from archdiscov.ingestion import aws
from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.collection_jobs import CollectionJob, collection_jobs
from archdiscov.ingestion.configuration_relationships import walk_resources
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.models.enums import AttachmentKind, AwsRegion, AwsService
from archdiscov.services import load_snapshot, save_snapshot
from tests.support.aws_fakes import Session as Ec2Session
from tests.support.ecs_data import Client as EcsClient
from tests.support.ecs_data import responses as ecs_responses
from tests.support.elbv2_data import Elbv2Client
from tests.support.elbv2_data import responses as elbv2_responses
from tests.support.iam_data import ACCOUNT, PROFILE_ARN, IamClient

from .test_elbv2_collection import ec2_pages
from .test_lambda_collection import Client as LambdaClient
from .test_lambda_collection import configuration
from .test_rds_collection import Client as RdsClient
from .test_rds_collection import data as rds_data
from .test_sqs_collection import Client as SqsClient

NOW = datetime(2026, 1, 1, tzinfo=UTC)
REGIONS = (AwsRegion.US_WEST_2, AwsRegion.US_EAST_1)


def in_region(value, region):
    if isinstance(value, str):
        return value.replace("us-east-1", region.value)
    if isinstance(value, list):
        return [in_region(item, region) for item in value]
    if isinstance(value, dict):
        return {key: in_region(item, region) for key, item in value.items()}
    return value


def denied(operation):
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "assembly-private-value"}}, operation
    )


class Session:
    def __init__(self, partial):
        self.iam = IamClient()
        if partial:
            self.iam.data["get_role"] = denied("GetRole")
        self.clients = {}
        self.pages = {}
        for region in REGIONS:
            pages = ec2_pages(region)
            instance = pages["describe_instances"][0]["Reservations"][0]["Instances"][0]
            instance["IamInstanceProfile"] = {"Arn": PROFILE_ARN}
            if partial:
                pages["describe_subnets"].append(denied("DescribeSubnets"))
                pages["describe_route_tables"] = [denied("DescribeRouteTables")]
            self.pages[region] = pages
            elbv2 = in_region(elbv2_responses(), region)
            if partial:
                elbv2["describe_target_health"] = denied("DescribeTargetHealth")
            function = in_region(configuration(), region)
            self.clients[region] = {
                "elbv2": Elbv2Client(elbv2),
                "ecs": EcsClient(in_region(ecs_responses(), region)),
                "lambda": LambdaClient(
                    pages=[{"Functions": [function]}],
                    detail=denied("GetFunctionConfiguration") if partial else function,
                ),
                "rds": RdsClient(in_region(rds_data(), region)),
                "sqs": SqsClient({"list_queues": {"QueueUrls": []}}),
            }
        self.ec2 = Ec2Session(pages=lambda region: self.pages[region])

    def client(self, name, **kwargs):
        if name in {"sts", "ec2"}:
            return self.ec2.client(name, **kwargs)
        if name == "iam":
            return self.iam
        return self.clients[kwargs["region_name"]][name]


class CapturingAdapter(aws.AwsAdapter):
    def _assemble_account(self, *args):
        self.inputs = args
        self.collected_before = dump_results(args[-2])
        return super()._assemble_account(*args)


def dump_results(results):
    return [
        {
            "job": result.job,
            "resources": [r.model_dump(mode="json") for r in result.collection.resources],
            "evidence": {
                k: e.model_dump(mode="json") for k, e in result.collection.evidence.items()
            },
            "coverage": [op.model_dump(mode="json") for op in result.collection.coverage],
            "dependencies": [link.model_dump(mode="json") for link in result.dependencies],
        }
        for result in results
    ]


@pytest.mark.parametrize("partial", [False, True])
def test_all_service_assembly_is_isolated_and_independent_of_result_order(
    partial, monkeypatch, tmp_path
):
    session = Session(partial)
    context = AdapterContext(
        clock=lambda: NOW,
        uuid_factory=lambda: UUID(int=174),
        session_factory=lambda **kwargs: session,
    )
    account = AwsAccountConfig(
        profile="offline", expected_account_id=ACCOUNT, regions=REGIONS, all_services=True
    )
    adapter = CapturingAdapter()
    real_collect = aws.collect_job
    lock = Lock()
    jobs_run = []

    def tracked_collect(job, *args):
        try:
            return real_collect(job, *args)
        finally:
            with lock:
                jobs_run.append(job)

    monkeypatch.setattr(aws, "collect_job", tracked_collect)
    (snapshot,) = adapter.collect(AwsCollectionRequest(accounts=(account,)), context=context)
    results = adapter.inputs[-2]
    assert len(results) == 13
    assert set(jobs_run) == {result.job for result in results}
    assert len(jobs_run) == len(results)
    assert sum(job.service == AwsService.IAM for job in jobs_run) == 1
    assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
    assert adapter.collected_before == dump_results(results)
    assert len(set().union(*(set(r.collection.evidence) for r in results))) == sum(
        len(r.collection.evidence) for r in results
    )

    expected = snapshot.model_dump(mode="json")
    shuffled = list(results)
    Random(174).shuffle(shuffled)
    for reordered in (list(reversed(results)), shuffled):
        args = (*adapter.inputs[:-2], reordered, adapter.inputs[-1])
        reassembled = aws.AwsAdapter()._assemble_account(*args)
        assert reassembled.model_dump(mode="json") == expected
        assert dump_results(results) == adapter.collected_before

    keys = [(op.region, op.service, op.operation) for op in snapshot.coverage.operations]
    assert len(keys) == len(set(keys))
    for region in REGIONS:
        resources = list(walk_resources(snapshot.regions[region].resources))
        assert len(resources) == len({r.id for r in resources})
        vpc = next(r for r in resources if r.identity.physical_id == "vpc-a")
        assert any(r.identity.service == AwsService.ELASTIC_LOAD_BALANCING for r in vpc.children)
        links = snapshot.dependencies_by_region[region]
        local_kinds = {link.attributes.get("kind") for link in links if link.target.kind == "local"}
        assert {
            AttachmentKind.INSTANCE_PROFILE,
            AttachmentKind.TASK_ROLE,
            AttachmentKind.SECURITY_GROUP_MEMBERSHIP,
            AttachmentKind.SUBNET_MEMBERSHIP,
            AttachmentKind.DATABASE_CLUSTER,
        } <= local_kinds
        assert any(
            link.relationship == "forwards_to" and link.target.kind == "local" for link in links
        )
        assert all(
            snapshot.evidence[eid].resource_id == link.source_id
            for link in links
            for eid in link.evidence_ids
        )
        coverage = {op.operation: op for op in snapshot.coverage.operations if op.region == region}
        assert coverage["ListQueues"].status == "success"
        assert coverage["ListQueues"].result_count == 0
        if partial:
            assert coverage["DescribeSubnets"].status == "incomplete"
            assert coverage["DescribeSubnets"].result_count == 1
            assert coverage["DescribeRouteTables"].status == "failed"
            assert coverage["DescribeTargetHealth"].status == "failed"
            assert coverage["GetFunctionConfiguration"].status == "failed"

    output = save_snapshot(snapshot, tmp_path / "account.json")
    assert load_snapshot(output).model_dump(mode="json") == expected
    assert "assembly-private-value" not in output.read_text()
    assert "protected-context" not in output.read_text()
    assert "protected-tag" not in output.read_text()
    # A snapshot caller can mutate its tree without altering collected job data.
    snapshot.regions[REGIONS[0]].resources[0].children.clear()
    assert dump_results(results) == adapter.collected_before


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unexpected"])
def test_assembly_rejects_missing_duplicate_or_unplanned_job_results(defect):
    account = AwsAccountConfig(
        profile="offline", expected_account_id=ACCOUNT, services=(AwsService.IAM,)
    )
    session = Session(False)
    context = AdapterContext(clock=lambda: NOW, session_factory=lambda **kwargs: session)
    adapter = CapturingAdapter()
    adapter.collect(AwsCollectionRequest(accounts=(account,)), context=context)
    results = deepcopy(adapter.inputs[-2])
    if defect == "missing":
        results.clear()
    elif defect == "duplicate":
        results.append(results[0])
    else:
        from dataclasses import replace

        results[0] = replace(results[0], job=CollectionJob(AwsService.EC2, REGIONS[0]))
    with pytest.raises(ValueError, match="each planned job exactly once"):
        aws.AwsAdapter()._assemble_account(*adapter.inputs[:-2], results, adapter.inputs[-1])


def test_job_planning_respects_selected_available_scope_and_global_single_pass():
    account = AwsAccountConfig(
        profile="offline", expected_account_id=ACCOUNT, regions=REGIONS, all_services=True
    )
    available = {service: REGIONS for service in account.services if service != AwsService.IAM}
    available[AwsService.LAMBDA] = (AwsRegion.US_EAST_1,)
    jobs = collection_jobs(account, available)
    assert len(jobs) == 12
    assert len(jobs) == len(set(jobs))
    assert [job for job in jobs if job.service == AwsService.IAM] == [
        CollectionJob(AwsService.IAM, AwsRegion.GLOBAL)
    ]
    assert CollectionJob(AwsService.LAMBDA, AwsRegion.US_WEST_2) not in jobs


def test_job_planning_never_adds_an_unselected_service():
    account = AwsAccountConfig(
        profile="offline",
        expected_account_id=ACCOUNT,
        regions=REGIONS,
        services=(AwsService.LAMBDA,),
    )
    available = {service: REGIONS for service in AwsService}
    assert collection_jobs(account, available) == [
        CollectionJob(AwsService.LAMBDA, region) for region in REGIONS
    ]
