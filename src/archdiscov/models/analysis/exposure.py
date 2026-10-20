from typing import Self

from pydantic import Field, model_validator

from ..enums import (
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    NetworkDecision,
    TargetHealthState,
)
from ..resource_base import Model
from .element_decisions import ElementDecision, validate_composition

# The elements that apply to each kind of entry point.
_KIND_ELEMENTS = {
    ExposureEntryKind.PUBLIC_ADDRESS: {
        ExposureElement.PUBLIC_ADDRESS,
        ExposureElement.ROUTE,
        ExposureElement.NACL,
        ExposureElement.SECURITY_GROUP,
    },
    ExposureEntryKind.LOAD_BALANCER: {
        ExposureElement.SCHEME,
        ExposureElement.ROUTE,
        ExposureElement.NACL,
        ExposureElement.SECURITY_GROUP,
        ExposureElement.LISTENER,
        ExposureElement.TARGET,
    },
}


class ExposureElementDecision(ElementDecision):
    """The network decision for one element of the exposure path, with its blocker codes."""

    element: ExposureElement


class InternetExposure(Model):
    """Candidate internet exposure of one entry point on a protocol and port range.

    The entry point (a resource with a recorded public IPv4 address, or an internet-facing
    load balancer) is the finding's first resource reference; targets reached through a
    load balancer follow it. This is recorded configuration only: never proof of
    reachability. `decision` composes the element decisions, listed once each in canonical
    order. `observed_target_health` records the health states observed for the reached
    targets as an observation only: it is never an element and never changes the decision.
    Every field is part of finding identity.
    """

    entry_kind: ExposureEntryKind
    protocol: ExposureProtocol
    port_start: int = Field(ge=1, le=65535, strict=True)
    port_end: int = Field(ge=1, le=65535, strict=True)
    decision: NetworkDecision
    elements: list[ExposureElementDecision] = Field(min_length=1)
    observed_target_health: list[TargetHealthState] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_exposure(self) -> Self:
        if self.port_start > self.port_end:
            raise ValueError("port_start must not exceed port_end")
        validate_composition(
            [item.element for item in self.elements],
            [item.decision for item in self.elements],
            self.decision,
            list(ExposureElement),
            _KIND_ELEMENTS[self.entry_kind],
        )
        if self.observed_target_health:
            if self.entry_kind != ExposureEntryKind.LOAD_BALANCER:
                raise ValueError("target health is observed only through a load balancer")
            if self.observed_target_health != sorted(set(self.observed_target_health)):
                raise ValueError("observed_target_health must be unique and sorted")
        return self
