"""The stored edges of a snapshot graph: containment and dependencies.

They are plain records built from validated snapshot models, kept here so that models,
such as query results, can name them without depending on the graph index.
"""

from dataclasses import dataclass

from pydantic import JsonValue

from .architecture import LocalTarget
from .cloud.contracts import CloudTarget
from .enums import AttachmentKind, DependencyRelationship


@dataclass(frozen=True)
class ContainmentEdge:
    """Stored parent-to-child placement, not an evidence-backed attachment."""

    source_id: str
    target_id: str

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return ()


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
