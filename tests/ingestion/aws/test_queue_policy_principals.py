from uuid import UUID

import pytest

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.iam.relationships import resolve_relationships as resolve_iam
from archdiscov.ingestion.queues.relationships import resolve_relationships
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    AwsSource,
    RegionArchitecture,
    SnapshotCoverage,
)
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.queues import SqsQueue
from tests.support.iam_data import ACCOUNT, ROLE_ARN, context

REGION = AwsRegion.US_EAST_1
SNAPSHOT = UUID(int=28)
URL = f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/work"
ARN = f"arn:aws:sqs:us-east-1:{ACCOUNT}:work"
POLICY = "Request[0].Attributes.Policy"
CONDITION = {"ArnEquals": {"aws:SourceArn": f"arn:aws:sns:us-east-1:{ACCOUNT}:topic"}}


def statement(principal, *, effect="Allow", negative=False, condition=True):
    data = {
        "Effect": effect,
        "Action": "sqs:SendMessage",
        "Resource": ARN,
        "NotPrincipal" if negative else "Principal": principal,
    }
    if condition:
        data["Condition"] = CONDITION
    return data


def queue_with(statements, *, cite=True, policy=True):
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
        policy=document if policy else None,
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


@pytest.mark.parametrize(
    ("principal", "target_kind", "principal_kind"),
    [
        ({"AWS": ROLE_ARN}, "local", "aws"),
        ({"AWS": "210987654321"}, "external", "aws"),
        ({"AWS": "arn:aws:iam::210987654321:root"}, "external", "aws"),
        ({"AWS": f"arn:aws:iam::{ACCOUNT}:role/not-stored"}, "external", "aws"),
        ({"Service": "sns.amazonaws.com"}, "external", "service"),
        ({"Federated": f"arn:aws:iam::{ACCOUNT}:saml-provider/company"}, "external", "federated"),
        ({"AWS": "*"}, "unresolved", "aws"),
        ("*", "unresolved", "unknown"),
        ({"CanonicalUser": "unsupported"}, "unresolved", "unknown"),
        ({"AWS": []}, "unresolved", "aws"),
    ],
)
def test_policy_principals_are_references_not_grants(principal, target_kind, principal_kind):
    queue, evidence, links = links_for([statement(principal, effect="Deny")])
    (link,) = links
    assert link.source_id == queue.id
    assert link.target.kind == target_kind
    assert link.relationship == "attached_to"
    assert link.attributes == {
        "kind": "resource_policy_principal",
        "principal_kind": principal_kind,
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


def test_not_principal_stays_unresolved_even_for_a_stored_role():
    _, _, (link,) = links_for([statement({"AWS": ROLE_ARN}, negative=True)])
    assert link.target.kind == "unresolved"
    assert link.target.source_path.endswith(".NotPrincipal.AWS")
    assert link.attributes["not_principal"] is True


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


@pytest.mark.parametrize(
    "options",
    [{"cite": False}, {"policy": False}, {"cite": False, "policy": False}],
    ids=["uncited", "no-document", "neither"],
)
def test_queues_without_a_cited_policy_emit_nothing(options):
    _, _evidence, links = links_for([statement({"AWS": "210987654321"})], **options)
    assert links == []


def test_queues_without_policies_and_other_resources_are_ignored():
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, REGION, AwsService.SQS)
    plain = builder._build(
        SqsQueue,
        operation="GetQueueAttributes",
        item_path="Attributes",
        item={},
        physical_id=ARN,
        attributes={},
        facts={},
        arn=ARN,
        queue_url=URL,
    )
    assert resolve_relationships(SNAPSHOT, ACCOUNT, [stored_role()], [plain], {}) == {}


def test_iam_trust_edges_are_unchanged_by_the_shared_extraction():
    builder = EvidenceBuilder(SNAPSHOT, ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    document = {"Statement": [statement({"AWS": "210987654321"}, condition=False)]}
    role = builder._build(
        IamRole,
        operation="GetRole",
        item_path="Role",
        item={},
        physical_id="AROA1",
        attributes={},
        facts={"/trust_document": ("AssumeRolePolicyDocument", document)},
        arn=ROLE_ARN,
        trust_document=document,
    )
    (link,) = resolve_iam(SNAPSHOT, ACCOUNT, [role], [], builder.evidence)[AwsRegion.GLOBAL]
    assert link.attributes["kind"] == "trust_principal"


def test_snapshot_with_policy_principals_validates_and_round_trips():
    queue, evidence, links = links_for(
        [statement({"AWS": "210987654321"}), statement({"AWS": "*"})]
    )
    snapshot = ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        regions={REGION: RegionArchitecture(resources=[queue])},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False),
        dependencies_by_region={REGION: links},
        evidence=evidence,
    )
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
