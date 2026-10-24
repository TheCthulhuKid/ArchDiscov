from datetime import datetime
from uuid import UUID

from archdiscov.models.cloud.contracts import CloudEvidence, CloudOperationCoverage
from archdiscov.models.cloud.scope import SupportedScope
from archdiscov.models.coverage_key import CoverageKey
from archdiscov.models.edges import ContainmentEdge, DependencyEdge
from archdiscov.models.enums import AttachmentKind, DependencyRelationship, SnapshotSourceKind
from archdiscov.models.resource_base import Resource
from archdiscov.models.snapshot_access import SnapshotAccess, SupportedSnapshot

from .coverage import CoverageIndex
from .dependencies import DependencyIndex, TerminalTarget
from .evidence import EvidenceIndex
from .identity import PhysicalResourceKey


class GraphIndex:
    """Own validated snapshot data and index stored resources and edges."""

    def __init__(self, snapshot: SupportedSnapshot) -> None:
        self._access = SnapshotAccess.from_snapshot(snapshot)
        self._resources = {resource.id: resource for resource in self._access._borrow_resources()}
        self._resource_ids = tuple(sorted(self._resources))
        self._physical_keys: dict[str, PhysicalResourceKey] = {}
        candidates: dict[PhysicalResourceKey, list[str]] = {}
        scope = self._access.scope
        for resource_id in self._resource_ids:
            key = PhysicalResourceKey.from_resource(scope, self._resources[resource_id])
            if key is not None:
                self._physical_keys[resource_id] = key
                candidates.setdefault(key, []).append(resource_id)
        self._physical = {key: tuple(ids) for key, ids in candidates.items()}
        self._containment_edges = tuple(
            ContainmentEdge(source_id, target_id)
            for source_id, target_id in sorted(self._access.containment())
        )
        self._parents = {edge.target_id: edge.source_id for edge in self._containment_edges}
        children: dict[str, list[str]] = {}
        for edge in self._containment_edges:
            children.setdefault(edge.source_id, []).append(edge.target_id)
        self._children = {key: tuple(ids) for key, ids in children.items()}
        self._root_ids = tuple(key for key in self._resource_ids if key not in self._parents)
        owned = self._access._borrow_snapshot()
        self._dependencies = DependencyIndex(owned.dependencies)
        self._evidence = EvidenceIndex(owned.evidence, self._resources)
        self._coverage = CoverageIndex(owned.coverage.operations)

    @property
    def snapshot_id(self) -> UUID:
        return self._access._borrow_snapshot().snapshot_id

    @property
    def source_kind(self) -> SnapshotSourceKind:
        """What the snapshot records: an observation, Terraform state or plan, or a template."""
        return self._access._borrow_snapshot().source.kind

    @property
    def source_label(self) -> str | None:
        """A readable name of the source (label, file path, or profile), never a credential."""
        provenance = self._access._borrow_snapshot().source.provenance
        for name in ("label", "path", "profile"):
            value = getattr(provenance, name, None)
            if value is not None:
                return str(value)
        return None

    @property
    def collection_window(self) -> tuple[datetime, datetime]:
        snapshot = self._access._borrow_snapshot()
        return snapshot.collection_started_at, snapshot.collection_completed_at

    @property
    def scope(self) -> SupportedScope:
        return self._access.scope

    @property
    def resource_ids(self) -> tuple[str, ...]:
        return self._resource_ids

    @property
    def root_ids(self) -> tuple[str, ...]:
        return self._root_ids

    @property
    def containment_edges(self) -> tuple[ContainmentEdge, ...]:
        return self._containment_edges

    def parent_id(self, resource_id: str) -> str | None:
        return self._parents.get(resource_id)

    def child_ids(self, resource_id: str) -> tuple[str, ...]:
        return self._children.get(resource_id, ())

    @property
    def dependency_edges(self) -> tuple[DependencyEdge, ...]:
        return self._dependencies.edges()

    @property
    def terminal_ids(self) -> tuple[str, ...]:
        return self._dependencies.terminal_ids

    def get_dependency_edge(self, edge_id: str) -> DependencyEdge | None:
        return self._dependencies.get_edge(edge_id)

    def get_terminal(self, terminal_id: str) -> TerminalTarget | None:
        return self._dependencies.get_terminal(terminal_id)

    def outgoing_edges(
        self,
        resource_id: str,
        *,
        relationship: DependencyRelationship | None = None,
        attachment_kind: AttachmentKind | None = None,
    ) -> tuple[DependencyEdge, ...]:
        return self._dependencies.adjacent(
            resource_id, relationship=relationship, attachment_kind=attachment_kind
        )

    def incoming_edges(
        self,
        target_id: str,
        *,
        relationship: DependencyRelationship | None = None,
        attachment_kind: AttachmentKind | None = None,
    ) -> tuple[DependencyEdge, ...]:
        return self._dependencies.adjacent(
            target_id, reverse=True, relationship=relationship, attachment_kind=attachment_kind
        )

    def get_resource(self, resource_id: str) -> Resource | None:
        resource = self._resources.get(resource_id)
        return resource.model_copy(deep=True) if resource is not None else None

    def resources(self) -> tuple[Resource, ...]:
        resources = {resource.id: resource for resource in self._access.resources()}
        return tuple(resources[key] for key in self._resource_ids)

    def physical_key(self, resource_id: str) -> PhysicalResourceKey | None:
        return self._physical_keys.get(resource_id)

    def find_physical(self, key: PhysicalResourceKey) -> tuple[Resource, ...]:
        return tuple(
            self._resources[item].model_copy(deep=True) for item in self._physical.get(key, ())
        )

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return self._evidence.ids

    def get_evidence(self, evidence_id: str) -> CloudEvidence | None:
        return self._evidence.get(evidence_id)

    def resource_evidence_ids(self, resource_id: str) -> tuple[str, ...]:
        return self._evidence.owner_ids(resource_id)

    def fact_evidence_ids(self, resource_id: str, pointer: str) -> tuple[str, ...]:
        return self._evidence.fact_ids(resource_id, pointer)

    def evidence_for_resource(self, resource_id: str) -> tuple[CloudEvidence, ...]:
        return self._evidence.records(self.resource_evidence_ids(resource_id))

    def evidence_for_fact(self, resource_id: str, pointer: str) -> tuple[CloudEvidence, ...]:
        return self._evidence.records(self.fact_evidence_ids(resource_id, pointer))

    @property
    def coverage_keys(self) -> tuple[CoverageKey, ...]:
        return self._coverage.keys

    def get_coverage(self, key: CoverageKey) -> CloudOperationCoverage | None:
        return self._coverage.get(key)

    def coverage_records(self) -> tuple[CloudOperationCoverage, ...]:
        return self._coverage.records()
