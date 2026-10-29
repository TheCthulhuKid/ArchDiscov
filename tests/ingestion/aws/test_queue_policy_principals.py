from uuid import UUID

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.queues.relationships import resolve_relationships
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.queues import SqsQueue
from tests.support.iam_data import ACCOUNT, ROLE_ARN

REGION = AwsRegion.US_EAST_1
SNAPSHOT = UUID(int=28)
URL = f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/work"
ARN = f"arn:aws:sqs:us-east-1:{ACCOUNT}:work"
POLICY = "Request[0].Attributes.Policy"
CONDITION = {"ArnEquals": {"aws:SourceArn": f"arn:aws:sns:us-east-1:{ACCOUNT}:topic"}}


def statement(principal, *, effect="Allow", condition=True):
    data = {
        "Effect": effect,
        "Action": "sqs:SendMessage",
        "Resource": ARN,
        "Principal": principal,
    }
    if condition:
        data["Condition"] = CONDITION
    return data


def queue_with(statements, *, cite=True):
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, REGION, AwsService.SQS)
    document = {"Version": "2012-10-17", "Statement": statements}
    queue = builder._build(
        SqsQueue,
        operation="GetQueueAttributes",
        item_path="Request[0].Attributes",
        item={"Policy": document},
        physical_id=ARN,
        attributes={},
        facts={"/policy": ("Policy", document)} if cite else {},
        arn=ARN,
        queue_url=URL,
        policy=document,
    )
    return queue, builder.evidence


def stored_role():
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    return builder._build(
        IamRole,
        operation="GetRole",
        item_path="Role",
        item={},
        physical_id="AROA1",
        attributes={},
        facts={},
        arn=ROLE_ARN,
    )


def links_for(statements, **options):
    queue, evidence = queue_with(statements, **options)
    links = resolve_relationships(SNAPSHOT, ACCOUNT, [stored_role()], [queue], evidence)
    return queue, evidence, links.get(REGION, [])


def test_policy_principals_are_references_not_grants():
    queue, evidence, links = links_for([statement({"AWS": ROLE_ARN}, effect="Deny")])
    (link,) = links
    assert link.source_id == queue.id
    assert link.target.kind == "local"
    assert link.relationship == "attached_to"
    assert link.attributes == {
        "kind": "resource_policy_principal",
        "principal_kind": "aws",
        "not_principal": False,
    }
    principal_evidence, statement_evidence = [evidence[key] for key in link.evidence_ids]
    assert statement_evidence.value["Effect"] == "Deny"
    assert statement_evidence.value["Condition"] == CONDITION
    assert principal_evidence.configuration_path.startswith(f"{POLICY}.Statement[0].Principal")
    assert all(e.resource_id == queue.id for e in (principal_evidence, statement_evidence))
    assert all(
        e.locator == "sqs:GetQueueAttributes" for e in (principal_evidence, statement_evidence)
    )


def test_every_statement_and_principal_entry_is_cited_in_order():
    _queue, evidence, links = links_for(
        [
            statement({"AWS": ["210987654321", f"arn:aws:iam::{ACCOUNT}:root"]}),
            statement({"Service": "sns.amazonaws.com"}, effect="Deny", condition=False),
        ]
    )
    assert [e.configuration_path for e in (evidence[link.evidence_ids[0]] for link in links)] == [
        f"{POLICY}.Statement[0].Principal.AWS[0]",
        f"{POLICY}.Statement[0].Principal.AWS[1]",
        f"{POLICY}.Statement[1].Principal.Service",
    ]
    assert evidence[links[2].evidence_ids[1]].value["Effect"] == "Deny"
    assert "Condition" not in evidence[links[2].evidence_ids[1]].value
    assert links[0].evidence_ids[1] == links[1].evidence_ids[1] != links[2].evidence_ids[1]


def test_a_single_statement_object_is_supported():
    queue, evidence = queue_with(statement({"AWS": "210987654321"}))
    (link,) = resolve_relationships(SNAPSHOT, ACCOUNT, [], [queue], evidence)[REGION]
    assert evidence[link.evidence_ids[1]].configuration_path == f"{POLICY}.Statement"


def test_a_policy_without_a_citation_emits_nothing():
    *_, links = links_for([statement({"AWS": "210987654321"})], cite=False)
    assert links == []
