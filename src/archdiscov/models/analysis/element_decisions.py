"""Shared shape of per-element network decisions in analyzer-specific finding blocks."""

from collections.abc import Collection, Sequence
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from ..enums import NetworkBlockerCode, NetworkDecision
from ..network_path import compose_decisions, validate_blocker_codes
from ..resource_base import Model


class ElementDecision(Model):
    """A network decision with its structured blocker codes.

    Blocker codes are unique and sorted; the cited detail lives in the finding's evidence,
    path, and limitations. Uses the shared network decision rules.
    """

    element: StrEnum  # narrowed to the analyzer's own element enum by each subclass
    decision: NetworkDecision
    blocker_codes: list[NetworkBlockerCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_codes(self) -> Self:
        validate_blocker_codes(self.decision, set(self.blocker_codes))
        if self.blocker_codes != sorted(set(self.blocker_codes)):
            raise ValueError("blocker_codes must be unique and sorted")
        return self


def validate_composition(
    kinds: Sequence[StrEnum],
    decisions: Sequence[NetworkDecision],
    composed: NetworkDecision,
    order: Sequence[StrEnum],
    allowed: Collection[StrEnum] | None = None,
) -> None:
    """Elements are unique, canonically ordered, allowed, and compose to the decision."""
    if list(kinds) != sorted(set(kinds), key=list(order).index):
        raise ValueError("elements must be unique and in canonical order")
    if allowed is not None and not set(kinds) <= set(allowed):
        raise ValueError("elements are not valid for this kind")
    if composed != compose_decisions(decisions):
        raise ValueError("decision must compose the element decisions")
