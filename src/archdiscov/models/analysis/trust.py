from typing import Self

from pydantic import Field, model_validator

from ..enums import TrustClassification, TrustEffect, TrustSource
from ..resource_base import Model


class CrossAccountTrust(Model):
    """One configured trust principal of the finding's first resource.

    The trusting resource is the finding's first resource reference, and the principal
    is the path step's target (or a limitation for unresolved forms). This records
    configured policy only: it never asserts that the principal can assume the role or
    reach the queue. Conditional trust means the statement carries a `Condition`;
    `NotPrincipal` statements name a complement that cannot be listed, so they are
    always classified unresolved. Every field is part of finding identity.
    """

    source: TrustSource
    classification: TrustClassification
    effect: TrustEffect
    conditional: bool = Field(strict=True)
    not_principal: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_combination(self) -> Self:
        if self.not_principal and self.classification != TrustClassification.UNRESOLVED:
            raise ValueError("NotPrincipal statements must be classified unresolved")
        return self
