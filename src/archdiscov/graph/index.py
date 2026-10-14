from uuid import UUID

from archdiscov.models.cloud_scope import SupportedScope
from archdiscov.models.resource_base import Resource
from archdiscov.services.snapshot_access import SnapshotAccess, SupportedSnapshot

from .containment import ContainmentEdge
from .identity import PhysicalResourceKey


class GraphIndex:
    """Own validated snapshot data and index resources and explicit containment."""

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

    @property
    def snapshot_id(self) -> UUID:
        return self._access._borrow_snapshot().snapshot_id

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
