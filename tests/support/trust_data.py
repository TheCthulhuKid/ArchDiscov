"""AWS snapshots whose roles carry trust statements, built through real IAM ingestion."""

from uuid import UUID

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.iam.relationships import resolve_relationships
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    Coverage,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.iam import IamRole
from tests.support.iam_data import ACCOUNT, context

SNAPSHOT = UUID(int=283)
OTHER = "210987654321"
CONDITION = {"StringEquals": {"sts:ExternalId": "partner"}}


def role_arn(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def statement(principal, *, effect="Allow", condition=False, negative=False) -> dict:
    data = {
        "Effect": effect,
        "Action": "sts:AssumeRole",
        "NotPrincipal" if negative else "Principal": principal,
    }
    if condition:
        data["Condition"] = CONDITION
    return data


def build(roles: dict[str, list[dict] | None], *, iam_status: str | None = None):
    """Roles by name to trust statements (None means no trust document) -> snapshot."""
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    resources = []
    for name, statements in roles.items():
        document = None if statements is None else {"Statement": statements}
        resources.append(
            builder._build(
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
    evidence = dict(builder.evidence)
    links = resolve_relationships(SNAPSHOT, ACCOUNT, resources, [], evidence)
    operations = []
    if iam_status:
        operations.append(iam_operation(iam_status))
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        global_resources=resources,
        coverage=SnapshotCoverage(regions=[], account_wide=True, operations=operations),
        dependencies_by_region=links,
        evidence=evidence,
    )


def iam_operation(status: str) -> Coverage:
    now = context(None).clock()
    data = {
        "account_id": ACCOUNT,
        "region": AwsRegion.GLOBAL,
        "service": AwsService.IAM,
        "operation": "GetRole",
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
