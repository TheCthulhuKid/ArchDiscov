"""Matching steps shared by the rule- and entry-based primitives.

A screening step answers whether a recorded rule or entry could match a query: True when
it still might, False when it surely cannot, or `Undecided` when the recorded data cannot
settle it. Only IPv4 and the protocols tcp, udp, and icmp are supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import TYPE_CHECKING

from archdiscov.ingestion.network.resources import FALLBACK_LIMITATION
from archdiscov.models.enums import NetworkBlockerCode
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource

from .network_decisions import blocker, deny, missing, unknown

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ALL_PROTOCOLS = "-1"
_PROTOCOLS = {"tcp": "tcp", "6": "tcp", "udp": "udp", "17": "udp", "icmp": "icmp", "1": "icmp"}


@dataclass(frozen=True)
class Undecided:
    """Why a rule or entry could not be matched or ruled out."""

    code: NetworkBlockerCode
    message: str

    @classmethod
    def missing(cls, message: str) -> Undecided:
        return cls(NetworkBlockerCode.MISSING_EVIDENCE, message)

    @classmethod
    def unsupported(cls, message: str) -> Undecided:
        return cls(NetworkBlockerCode.UNSUPPORTED_ELEMENT, message)

    def result(self, *, element: str, resource_id: str) -> NetworkResult:
        return unknown([blocker(self.code, self.message, element=element, resource_id=resource_id)])


Screen = bool | Undecided


def combine(*screens: Screen) -> Screen:
    """The first screen that is not True, else True: every step must keep the match open."""
    return next((screen for screen in screens if screen is not True), True)


def normalize_protocol(value: str | None) -> str | None:
    """tcp, udp, or icmp for a name or number; None when absent or unrecognized."""
    return _PROTOCOLS.get(value.lower()) if value is not None else None


def screen_protocol(
    protocol: str | None, from_port: int | None, to_port: int | None, query: str, port: int
) -> Screen:
    """Whether a recorded protocol and inclusive port range can hold the normalized query."""
    if protocol is None:
        return Undecided.missing("The rule or entry records no protocol.")
    if protocol == ALL_PROTOCOLS:
        return True
    recognized = normalize_protocol(protocol)
    if recognized is None:
        return Undecided.unsupported("The rule or entry uses an unrecognized protocol.")
    if recognized != query:
        return False
    if from_port is None or to_port is None:
        return Undecided.missing("The rule or entry records no port range.")
    return from_port == -1 or from_port <= port <= to_port


def screen_cidr(cidr: object, network: IPv4Network | None, unknown_message: str) -> Screen:
    """Whether the recorded IPv4 CIDR contains the queried network; IPv6 is unsupported."""
    if not isinstance(cidr, IPv4Network):
        return Undecided.unsupported("IPv6 rules and entries are unsupported.")
    if network is None:
        return Undecided.missing(unknown_message)
    return network.subnet_of(cidr)


def omitted_rules(resource: Resource) -> bool:
    """Whether normalization dropped rules or entries, so absent matches prove nothing.

    The rule-ID fallback note is excluded: it loses identifiers, not rules.
    """
    limitations = resource.attributes.get("limitations") or []
    return any(item != FALLBACK_LIMITATION for item in limitations)


def implicit_deny(
    index: GraphIndex,
    resource: Resource,
    reason: str,
    extra_evidence_ids: tuple[str, ...] = (),
) -> NetworkResult:
    """A deny citing the resource's own evidence, or unknown when it has none."""
    own_evidence_ids = index.resource_evidence_ids(resource.id)
    if not own_evidence_ids:
        return missing("The resource has no recorded evidence.", resource_id=resource.id)
    return deny(
        [
            blocker(
                NetworkBlockerCode.DENIED,
                reason,
                element="implicit_deny",
                resource_id=resource.id,
                evidence_ids=(*own_evidence_ids, *extra_evidence_ids),
            )
        ]
    )
