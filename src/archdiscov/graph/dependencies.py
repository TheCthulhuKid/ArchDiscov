import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass

from pydantic import JsonValue

from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import CloudDependency, CloudExternalTarget, CloudTarget
from archdiscov.models.enums import AttachmentKind, DependencyRelationship


@dataclass(frozen=True)
class DependencyEdge:
    """A stored directed dependency; nested values returned publicly are detached."""

    id: str
    source_id: str
    target: CloudTarget
    location: str | None
    relationship: DependencyRelationship
    attributes: dict[str, JsonValue]
    evidence_ids: tuple[str, ...]

    @property
    def target_id(self) -> str:
        if isinstance(self.target, LocalTarget):
            return self.target.resource_id
        return f"terminal/{self.id}"

    @property
    def attachment_kind(self) -> AttachmentKind | None:
        kind = self.attributes.get("kind")
        if not isinstance(kind, str):
            return None
        try:
            return AttachmentKind(kind)
        except ValueError:
            return None


@dataclass(frozen=True)
class TerminalTarget:
    """Nonlocal reference belonging to one edge, never a stored resource."""

    id: str
    edge_id: str
    target: CloudExternalTarget | UnresolvedTarget


AdjacencyKey = tuple[str, DependencyRelationship | None, AttachmentKind | None]


class DependencyIndex:
    """Private owned edge data with detached public lookup results."""

    def __init__(self, dependencies: Iterable[CloudDependency]) -> None:
        records = sorted(
            [
                (
                    json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                    item,
                )
                for item in dependencies
            ],
            key=lambda record: record[0],
        )
        self._edges: dict[str, DependencyEdge] = {}
        self._terminals: dict[str, TerminalTarget] = {}
        outgoing: dict[AdjacencyKey, list[str]] = {}
        incoming: dict[AdjacencyKey, list[str]] = {}
        occurrences: dict[str, int] = {}
        for data, dependency in records:
            digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
            ordinal = occurrences.get(digest, 0)
            occurrences[digest] = ordinal + 1
            edge_id = f"dependency/{digest}/{ordinal}"
            edge = DependencyEdge(
                edge_id,
                dependency.source_id,
                dependency.target.model_copy(deep=True),
                dependency.location,
                dependency.relationship,
                deepcopy(dependency.attributes),
                tuple(dependency.evidence_ids),
            )
            self._edges[edge_id] = edge
            if not isinstance(edge.target, LocalTarget):
                self._terminals[edge.target_id] = TerminalTarget(
                    edge.target_id, edge_id, edge.target
                )
            self._add(outgoing, edge.source_id, edge)
            self._add(incoming, edge.target_id, edge)
        self._outgoing = {key: tuple(ids) for key, ids in outgoing.items()}
        self._incoming = {key: tuple(ids) for key, ids in incoming.items()}
        self._terminal_ids = tuple(sorted(self._terminals))

    @staticmethod
    def _add(adjacency: dict[AdjacencyKey, list[str]], node_id: str, edge: DependencyEdge) -> None:
        keys = {(node_id, None, None), (node_id, edge.relationship, None)}
        kind = edge.attachment_kind
        if kind is not None:
            keys.update(
                {
                    (node_id, None, kind),
                    (node_id, edge.relationship, kind),
                }
            )
        for key in keys:
            adjacency.setdefault(key, []).append(edge.id)

    def edges(self) -> tuple[DependencyEdge, ...]:
        return tuple(deepcopy(edge) for edge in self._edges.values())

    def get_edge(self, edge_id: str) -> DependencyEdge | None:
        return deepcopy(self._edges.get(edge_id))

    @property
    def terminal_ids(self) -> tuple[str, ...]:
        return self._terminal_ids

    def get_terminal(self, terminal_id: str) -> TerminalTarget | None:
        return deepcopy(self._terminals.get(terminal_id))

    def adjacent(
        self,
        node_id: str,
        *,
        reverse: bool = False,
        relationship: DependencyRelationship | None = None,
        attachment_kind: AttachmentKind | None = None,
    ) -> tuple[DependencyEdge, ...]:
        adjacency = self._incoming if reverse else self._outgoing
        ids = adjacency.get((node_id, relationship, attachment_kind), ())
        return tuple(deepcopy(self._edges[edge_id]) for edge_id in ids)
