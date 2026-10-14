from typing import Annotated, Literal

from pydantic import Field, JsonValue

from .architecture import UnresolvedTarget
from .cloud_contracts import CloudTarget
from .cloud_scope import SupportedScope
from .enums import DependencyRelationship
from .resource_base import Model, NonEmptyString


class ResourceReference(Model):
    resource_id: NonEmptyString
    scope: SupportedScope


class CoverageReference(Model):
    scope: SupportedScope
    location: NonEmptyString | None = None
    service: NonEmptyString
    operation: NonEmptyString


class DependencyPathStep(Model):
    """Snapshot dependency claims; matching and citation subsets require snapshot validation."""

    kind: Literal["dependency"] = "dependency"
    source_id: NonEmptyString
    target: CloudTarget
    relationship: DependencyRelationship
    evidence_ids: list[NonEmptyString] = Field(min_length=1)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ContainmentPathStep(Model):
    """Structural containment, not a synthesized attachment; citations are source-owned."""

    kind: Literal["containment"] = "containment"
    source_id: NonEmptyString
    target_id: NonEmptyString
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)


PathStep = Annotated[DependencyPathStep | ContainmentPathStep, Field(discriminator="kind")]


class UnresolvedTargetReference(Model):
    source_id: NonEmptyString
    target: UnresolvedTarget
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class AnalysisLimitation(Model):
    code: NonEmptyString
    message: NonEmptyString
    coverage: CoverageReference | None = None
    unresolved_target: UnresolvedTargetReference | None = None
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
