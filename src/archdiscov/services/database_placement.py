"""Network placement of a selected RDS instance or cluster, from recorded structure.

Placement is a set: a database has no single recorded address, so subnets and security
groups are kept as stored resource IDs for conservative evaluation. A cluster takes the
placement of its stored member instances and its subnet group. Everything missing is
reported as a limitation, never as absence of access, and no value is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.architecture import LocalTarget, Vpc
from archdiscov.models.enums import AnalyzerErrorCode, AttachmentKind
from archdiscov.models.resources.database import DbCluster, DbInstance, DbSubnetGroup

from .analysis_limitations import target_limitations
from .analyzer_errors import AnalyzerError

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

Database = DbInstance | DbCluster
Owner = Database | DbSubnetGroup


class PortStatus(StrEnum):
    """How the requested port compares with the recorded endpoint ports."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class DatabasePlacement:
    """Recorded placement of the selected database; IDs are stored resource IDs.

    `evidence_ids` cite the placement facts used (memberships, ports); `limitations`
    explain every missing, unresolved, or conflicting element.
    """

    database: Database
    members: tuple[DbInstance, ...]
    vpc_id: str | None
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    port_status: PortStatus
    recorded_ports: tuple[int, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]


def _limitation(code: str, message: str, evidence_ids: tuple[str, ...] = ()) -> AnalysisLimitation:
    return AnalysisLimitation(code=code, message=message, evidence_ids=list(evidence_ids))


class _Collector:
    """Accumulates placement sets, citations, and limitations in deterministic order."""

    def __init__(self, index: GraphIndex) -> None:
        self.index = index
        self.evidence: set[str] = set()
        self.limitations: dict[tuple, AnalysisLimitation] = {}

    def note(self, limitation: AnalysisLimitation) -> None:
        key = (limitation.code, limitation.message, tuple(limitation.evidence_ids))
        self.limitations.setdefault(key, limitation)

    def memberships(self, owners: list[Owner], kind: AttachmentKind, noun: str) -> set[str]:
        """Stored targets of the owners' membership edges; others become limitations."""
        found: set[str] = set()
        for owner in owners:
            for edge in self.index.outgoing_edges(owner.id, attachment_kind=kind):
                self.evidence.update(edge.evidence_ids)
                if isinstance(edge.target, LocalTarget):
                    found.add(edge.target_id)
                else:
                    for limitation in target_limitations(edge, noun):
                        self.note(limitation)
        return found


def _subnet_group_edges(index: GraphIndex, owners: list[Database]) -> list[DependencyEdge]:
    return [
        edge
        for owner in owners
        for edge in index.outgoing_edges(
            owner.id, attachment_kind=AttachmentKind.DATABASE_SUBNET_GROUP
        )
    ]


def _stored_subnet_groups(index: GraphIndex, edges: list[DependencyEdge]) -> list[DbSubnetGroup]:
    groups = (
        index.get_resource(edge.target_id) for edge in edges if edge.target_id in index.resource_ids
    )
    return [group for group in groups if isinstance(group, DbSubnetGroup)]


def _member_instances(
    index: GraphIndex, cluster: DbCluster, collector: _Collector
) -> tuple[DbInstance, ...]:
    """Stored instances of the cluster; a recorded member that is not stored is a limitation."""
    stored = {
        edge.source_id: instance
        for edge in index.incoming_edges(
            cluster.id, attachment_kind=AttachmentKind.DATABASE_CLUSTER
        )
        if isinstance(instance := index.get_resource(edge.source_id), DbInstance)
    }
    if cluster.members is None:
        collector.note(
            _limitation(
                "database_members_unrecorded", "The cluster's member instances are not recorded."
            )
        )
    else:
        identifiers = {instance.identifier for instance in stored.values()}
        for member in cluster.members:
            if member.db_instance_identifier not in identifiers:
                collector.note(
                    _limitation(
                        "database_member_unrecorded",
                        f"Cluster member {member.db_instance_identifier} is not stored.",
                        tuple(cluster.fact_evidence.get("/members", [])),
                    )
                )
    return tuple(stored[key] for key in sorted(stored))


def _owner_evidence(owners: list[Database], pointer: str) -> tuple[str, ...]:
    return tuple(e for owner in owners for e in owner.fact_evidence.get(pointer, []))


def _ports(
    owners: list[Database], port: int, collector: _Collector
) -> tuple[PortStatus, tuple[int, ...]]:
    recorded = tuple(
        sorted({owner.endpoint_port for owner in owners if owner.endpoint_port is not None})
    )
    evidence = _owner_evidence(owners, "/endpoint_port")
    collector.evidence.update(evidence)
    if not recorded:
        collector.note(_limitation("database_port_unrecorded", "No endpoint port is recorded."))
        return PortStatus.UNRECORDED, recorded
    if port in recorded:
        return PortStatus.MATCH, recorded
    collector.note(
        _limitation(
            "database_port_mismatch",
            f"The requested port {port} differs from the recorded port(s) "
            f"{', '.join(map(str, recorded))}.",
            evidence,
        )
    )
    return PortStatus.MISMATCH, recorded


def _vpc(
    index: GraphIndex, owners: list[Database], subnet_ids: set[str], collector: _Collector
) -> str | None:
    """The one stored VPC that contains the subnets, checked against the recorded VPC ID."""
    parents = (index.get_resource(index.parent_id(subnet) or "") for subnet in subnet_ids)
    vpcs = {parent.id: parent for parent in parents if isinstance(parent, Vpc)}
    if len(vpcs) != 1:
        collector.note(
            _limitation(
                "database_vpc_ambiguous" if vpcs else "database_vpc_unresolved",
                "The database's subnets do not resolve to exactly one stored VPC.",
            )
        )
        return None
    [(vpc_id, vpc)] = vpcs.items()
    recorded = {owner.vpc_id for owner in owners if owner.vpc_id is not None}
    if recorded - {vpc.identity.physical_id}:
        collector.note(
            _limitation(
                "database_vpc_mismatch",
                "The recorded VPC ID differs from the VPC containing the subnets.",
                _owner_evidence(owners, "/vpc_id"),
            )
        )
        return None
    return vpc_id


def resolve_database_placement(index: GraphIndex, resource_id: str, port: int) -> DatabasePlacement:
    """Resolve the selected RDS instance or cluster to its recorded placement.

    An unknown resource raises `RESOURCE_NOT_FOUND` and anything other than an RDS
    instance or cluster `UNSUPPORTED_RESOURCE`, as the other analyzers do. Callers handle
    non-AWS snapshots before resolving.
    """
    selected = index.get_resource(resource_id)
    if selected is None:
        raise AnalyzerError(AnalyzerErrorCode.RESOURCE_NOT_FOUND)
    if not isinstance(selected, (DbInstance, DbCluster)):
        raise AnalyzerError(AnalyzerErrorCode.UNSUPPORTED_RESOURCE)
    collector = _Collector(index)
    members = (
        _member_instances(index, selected, collector) if isinstance(selected, DbCluster) else ()
    )
    owners: list[Database] = [selected, *members]
    group_edges = _subnet_group_edges(index, owners)
    groups = _stored_subnet_groups(index, group_edges)
    subnet_ids = collector.memberships(
        [*owners, *groups], AttachmentKind.SUBNET_MEMBERSHIP, "subnet"
    )
    security_groups = collector.memberships(
        owners, AttachmentKind.SECURITY_GROUP_MEMBERSHIP, "security group"
    )
    if not subnet_ids:
        collector.note(
            _limitation("database_subnets_unrecorded", "No subnet placement is recorded.")
        )
        for edge in group_edges:
            collector.evidence.update(edge.evidence_ids)
            for limitation in target_limitations(edge, "DB subnet group"):
                collector.note(limitation)
    if not security_groups:
        collector.note(
            _limitation("database_security_groups_unrecorded", "No security group is recorded.")
        )
    status, recorded_ports = _ports(owners, port, collector)
    vpc = _vpc(index, owners, subnet_ids, collector) if subnet_ids else None
    return DatabasePlacement(
        database=selected,
        members=members,
        vpc_id=vpc,
        subnet_ids=tuple(sorted(subnet_ids)),
        security_group_ids=tuple(sorted(security_groups)),
        port_status=status,
        recorded_ports=recorded_ports,
        evidence_ids=tuple(sorted(collector.evidence)),
        limitations=tuple(collector.limitations[key] for key in sorted(collector.limitations)),
    )
