"""Observed SQS queue-policy principals, not effective access to a queue."""

from collections.abc import Iterable
from uuid import UUID

from ...models.architecture import Dependency, Evidence, Resource
from ...models.enums import (
    AttachmentKind,
    AwsRegion,
    AwsService,
    DependencyRelationship,
)
from ...models.resource_tree import walk_resources
from ...models.resources.queues import SqsQueue
from ..evidence_builder import EvidenceBuilder
from ..policy_principals import arn_groups, policy_principal_links


def resolve_relationships(
    snapshot_id: UUID,
    account_id: str,
    global_resources: list[Resource],
    regional_resources: Iterable[Resource],
    evidence: dict[str, Evidence],
) -> dict[AwsRegion, list[Dependency]]:
    """One dependency per principal of each stored queue-policy statement.

    Uses the policy and citation queue collection already recorded, with the same
    principal rules as IAM trust policies. Queues without a cited, sanitized policy emit
    nothing; Effect and Condition remain only in the cited statement evidence.
    """
    groups = arn_groups(global_resources)
    builders: dict[AwsRegion, EvidenceBuilder] = {}
    dependencies: dict[AwsRegion, list[Dependency]] = {}
    for queue in walk_resources(regional_resources):
        if not isinstance(queue, SqsQueue):
            continue
        region = queue.identity.region
        builder = builders.setdefault(
            region, EvidenceBuilder(snapshot_id, account_id, region, AwsService.SQS)
        )
        citations = queue.fact_evidence.get("/policy")

        def emit(target, citations, queue=queue, region=region, **attributes):
            dependencies.setdefault(region, []).append(
                Dependency(
                    source_id=queue.id,
                    target=target,
                    relationship=DependencyRelationship.ATTACHED_TO,
                    attributes={
                        "kind": AttachmentKind.RESOURCE_POLICY_PRINCIPAL.value,
                        **attributes,
                    },
                    evidence_ids=citations,
                )
            )

        policy_principal_links(
            queue,
            queue.policy,
            evidence[citations[0]] if citations else None,
            builder,
            groups,
            emit,
        )
    for builder in builders.values():
        evidence.update(builder.evidence)
    return dependencies
