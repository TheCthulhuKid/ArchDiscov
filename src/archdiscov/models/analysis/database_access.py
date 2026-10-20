from typing import Self

from pydantic import Field, model_validator

from ..enums import (
    AccessElement,
    AccessSourceKind,
    NetworkBlockerCode,
    NetworkDecision,
)
from ..network_path import compose_decisions, validate_blocker_codes
from ..resource_base import Model


class AccessElementDecision(Model):
    """The network decision for one element of the path, with its blocker codes.

    Blocker codes are structured and sorted; the cited detail lives in the finding's
    evidence, path, and limitations. Uses the shared network decision rules.
    """

    element: AccessElement
    decision: NetworkDecision
    blocker_codes: list[NetworkBlockerCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_codes(self) -> Self:
        validate_blocker_codes(self.decision, set(self.blocker_codes))
        if self.blocker_codes != sorted(set(self.blocker_codes)):
            raise ValueError("blocker_codes must be unique and sorted")
        return self


class DatabaseAccess(Model):
    """One compute source's potential network path to the selected database port.

    The source is the finding's first resource reference and the selected database its
    second. This is network permission from recorded configuration only: it never
    asserts authentication, credentials, IAM database access, or live reachability.
    `decision` composes the element decisions, which are listed once each in canonical
    order. Every field is part of finding identity.
    """

    source_kind: AccessSourceKind
    port: int = Field(ge=1, le=65535, strict=True)
    decision: NetworkDecision
    elements: list[AccessElementDecision] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_elements(self) -> Self:
        order = list(AccessElement)
        kinds = [item.element for item in self.elements]
        if kinds != sorted(set(kinds), key=order.index):
            raise ValueError("elements must be unique and in canonical order")
        if self.decision != compose_decisions([item.decision for item in self.elements]):
            raise ValueError("decision must compose the element decisions")
        return self
