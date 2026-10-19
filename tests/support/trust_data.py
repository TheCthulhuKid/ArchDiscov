"""AWS snapshots whose roles and queues carry policies, built through real ingestion code."""

from uuid import UUID

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.iam.relationships import resolve_relationships
from archdiscov.ingestion.queues.relationships import resolve_relationships as resolve_queues
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    Coverage,
    Evidence,
    RegionArchitecture,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsRegion, AwsService, EvidenceValueStatus, SourceType
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.queues import SqsQueue
from tests.support.iam_data import ACCOUNT, context

SNAPSHOT = UUID(int=283)
OTHER = "210987654321"
REGION = AwsRegion.US_EAST_1
CONDITION = {"StringEquals": {"sts:ExternalId": "partner"}}


def role_arn(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def queue_arn(name: str) -> str:
    return f"arn:aws:sqs:{REGION.value}:{ACCOUNT}:{name}"


def statement(principal, *, effect="Allow", condition=False, negative=False) -> dict:
    data = {
        "Effect": effect,
        "Action": "sts:AssumeRole",
        "NotPrincipal" if negative else "Principal": principal,
    }
    if condition:
        data["Condition"] = CONDITION
    return data


def operation(service: AwsService, region: AwsRegion, status: str, name: str) -> Coverage:
    now = context(None).clock()
    data = {
        "account_id": ACCOUNT,
        "region": region,
        "service": service,
        "operation": name,
        "status": status,
        "started_at": now,
        "completed_at": now,
    }
    if status == "success":
        data["result_count"] = 1
    elif status == "failed":
        data["failures"] = [{"code": "AccessDenied", "message": "Denied"}]
    else:
        data["reason"] = "Not collected"
    return Coverage.model_validate(data)


def build(
    roles: dict[str, list[dict] | None] | None = None,
    queues: dict[str, list[dict] | str | None] | None = None,
    *,
    iam_status: str | None = "success",
    sqs_status: str | None = "success",
):
    """Roles and queues by name -> snapshot.

    A role maps to its trust statements (None: no trust document). A queue maps to its
    policy statements, None for no policy, or "omitted" for an unreadable policy. A status
    of None records no coverage for that service.
    """
    iam = EvidenceBuilder(SNAPSHOT, ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    resources = []
    for name, statements in (roles or {}).items():
        document = None if statements is None else {"Statement": statements}
        resources.append(
            iam._build(
                IamRole,
                operation="GetRole",
                item_path=f'Request[{{"RoleName":"{name}"}}].Role',
                item={"AssumeRolePolicyDocument": document},
                physical_id=f"AROA{name}",
                attributes={},
                facts={} if document is None else {"/trust_document": ("Trust", document)},
                arn=role_arn(name),
                trust_document=document,
            )
        )
    sqs = EvidenceBuilder(SNAPSHOT, ACCOUNT, REGION, AwsService.SQS)
    queue_resources = []
    for name, statements in (queues or {}).items():
        document = {"Statement": statements} if isinstance(statements, list) else None
        queue = sqs._build(
            SqsQueue,
            operation="GetQueueAttributes",
            item_path="Request[0].Attributes",
            item={"Policy": document},
            physical_id=queue_arn(name),
            attributes={},
            facts={"/policy": ("Policy", document)} if document else {},
            arn=queue_arn(name),
            queue_url=f"https://sqs.{REGION.value}.amazonaws.com/{ACCOUNT}/{name}",
            policy=document,
        )
        if statements == "omitted":
            key = f"omitted/{name}"
            sqs.evidence[key] = Evidence(
                snapshot_id=SNAPSHOT,
                resource_id=queue.id,
                source=SourceType.AWS,
                locator="sqs:GetQueueAttributes",
                configuration_path="Request[0].Attributes.Policy",
                value_status=EvidenceValueStatus.OMITTED,
                limitation="Configuration value unavailable or malformed",
            )
            queue.evidence_ids.append(key)
        queue_resources.append(queue)
    evidence = {**iam.evidence, **sqs.evidence}
    links = resolve_relationships(SNAPSHOT, ACCOUNT, resources, queue_resources, evidence)
    for region, items in resolve_queues(
        SNAPSHOT, ACCOUNT, resources, queue_resources, evidence
    ).items():
        links.setdefault(region, []).extend(items)
    operations = []
    if iam_status:
        operations.append(operation(AwsService.IAM, AwsRegion.GLOBAL, iam_status, "GetRole"))
    if sqs_status:
        operations.append(operation(AwsService.SQS, REGION, sqs_status, "GetQueueAttributes"))
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        regions={REGION: RegionArchitecture(resources=queue_resources)} if queue_resources else {},
        global_resources=resources,
        coverage=SnapshotCoverage(
            regions=[REGION] if queue_resources or sqs_status else [],
            account_wide=True,
            operations=operations,
        ),
        dependencies_by_region=links,
        evidence=evidence,
    )


def schema_one(snapshot: ArchitectureSnapshot) -> ArchitectureSnapshot:
    """The same snapshot as schema 1, which can only hold local dependency `target_id`s."""
    from archdiscov.services import validate_snapshot

    data = snapshot.model_dump(mode="json")
    data["schema_version"] = "1"
    for region, edges in data["dependencies_by_region"].items():
        data["dependencies_by_region"][region] = [
            {
                **{k: v for k, v in edge.items() if k != "target"},
                "target_id": edge["target"]["resource_id"],
            }
            for edge in edges
            if edge["target"]["kind"] == "local"
        ]
    return validate_snapshot(data)
