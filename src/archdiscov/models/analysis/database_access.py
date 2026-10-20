from typing import Self

from pydantic import Field, model_validator

from ..enums import AccessElement, AccessSourceKind, NetworkDecision
from ..resource_base import Model
from .element_decisions import ElementDecision, validate_composition


class AccessElementDecision(ElementDecision):
    """The network decision for one element of the database path, with its blocker codes."""

    element: AccessElement


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
        validate_composition(
            [item.element for item in self.elements],
            [item.decision for item in self.elements],
            self.decision,
            list(AccessElement),
        )
        return self
