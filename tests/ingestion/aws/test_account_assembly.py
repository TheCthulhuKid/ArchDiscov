from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from archdiscov.ingestion import aws
from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.collection_jobs import CollectionJob, collection_jobs
from archdiscov.ingestion.configuration_relationships import walk_resources
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.models.enums import AttachmentKind, AwsRegion, AwsService
from archdiscov.services import load_snapshot, save_snapshot
from tests.support.account_assembly import REGIONS, Session
from tests.support.iam_data import ACCOUNT

NOW = datetime(2026, 1, 1, tzinfo=UTC)


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


def test_all_service_assembly_is_isolated_and_independent_of_result_order(monkeypatch, tmp_path):
    session = Session(partial=True)
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
    args = (*adapter.inputs[:-2], list(reversed(results)), adapter.inputs[-1])
    assert aws.AwsAdapter()._assemble_account(*args).model_dump(mode="json") == expected
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
        assert coverage["DescribeSubnets"].status == "incomplete"
        assert coverage["DescribeSubnets"].result_count == 1
        assert coverage["DescribeRouteTables"].status == "failed"
        assert coverage["DescribeTargetHealth"].status == "failed"
        assert coverage["GetFunctionConfiguration"].status == "failed"

    output = save_snapshot(snapshot, tmp_path / "account.json")
    assert load_snapshot(output).model_dump(mode="json") == expected
    saved = output.read_text()
    for private in ("assembly-private-value", "protected-context", "protected-tag"):
        assert private not in saved


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
    lambda_only = AwsAccountConfig(
        profile="offline", expected_account_id=ACCOUNT, regions=REGIONS, services=("lambda",)
    )
    assert collection_jobs(lambda_only, dict.fromkeys(AwsService, REGIONS)) == [
        CollectionJob(AwsService.LAMBDA, region) for region in REGIONS
    ]
