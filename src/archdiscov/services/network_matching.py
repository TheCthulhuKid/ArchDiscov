"""Matching steps shared by the rule- and entry-based primitives.

A screening step answers whether a recorded rule or entry could match a query: True when
it still might, False when it surely cannot, or `Undecided` when the recorded data cannot
settle it. Addresses are IPv4 or IPv6 and are compared within one address family; only
the protocols tcp, udp, and icmp are supported.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import TYPE_CHECKING

from archdiscov.ingestion.network.resources import FALLBACK_LIMITATION
from archdiscov.models.enums import AttachmentKind, NetworkBlockerCode
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource

from .network_decisions import allow, blocker, deny, missing, require_aws, unknown

if TYPE_CHECKING:
    from archdiscov.graph import DependencyEdge, GraphIndex

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


type IpNetwork = IPv4Network | IPv6Network


def parse_network(value: str) -> IpNetwork:
    """An IPv4 or IPv6 address (a /32 or /128) or CIDR; raises ValueError otherwise."""
    return ip_network(value, strict=False)


def screen_cidr(
    cidr: IpNetwork | None,
    network: IpNetwork | None,
    unknown_message: str,
    *,
    bounded: bool = False,
) -> Screen:
    """Whether the recorded CIDR contains the queried network of the same address family.

    A CIDR of the other family can never match. With `bounded`, the network only bounds an
    address that is not known exactly: a CIDR that covers part of it cannot be matched or
    ruled out, so it is undecided.
    """
    if cidr is None:
        return Undecided.missing("The rule or entry records no CIDR.")
    if network is None:
        return Undecided.missing(unknown_message)
    if network.version != cidr.version:
        return False
    if network.subnet_of(cidr):
        return True
    if bounded and network.overlaps(cidr):
        return Undecided.missing("The address is only known to lie in a network this overlaps.")
    return False


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


def sole_associate[R: Resource](
    index: GraphIndex,
    target: Resource,
    kind: AttachmentKind,
    expected: type[R],
    label: str,
    *,
    only: Callable[[DependencyEdge], bool] = lambda edge: True,
) -> tuple[R, tuple[str, ...]] | NetworkResult:
    """The one observed associate attached to `target`, with the association's evidence.

    None, several, an unrecorded or wrongly typed associate, or one with omitted rules
    give an unknown result: no default is ever assumed.
    """
    edges = [edge for edge in index.incoming_edges(target.id, attachment_kind=kind) if only(edge)]
    element, noun = f"{label}_association", label.replace("_", " ")

    def problem(message: str, resource_id: str) -> NetworkResult:
        return missing(message, element=element, resource_id=resource_id)

    if not edges:
        return problem(f"No {noun} association is recorded.", target.id)
    if len(edges) > 1:
        return problem(f"Several {noun} associations are recorded.", target.id)
    associate = index.get_resource(edges[0].source_id)
    if not isinstance(associate, expected):
        return problem(f"The associated {noun} is not recorded.", target.id)
    if omitted_rules(associate):
        return problem(f"Some {noun} entries could not be recorded.", associate.id)
    return associate, edges[0].evidence_ids


def cited_decision(
    allowed: bool,
    citations: list[str],
    extra_evidence_ids: tuple[str, ...],
    *,
    subject: str,
    element: str,
    resource_id: str,
) -> NetworkResult:
    """The cited allow or deny of a matched rule, entry, or route; uncited is unknown."""
    if not citations:
        return missing(
            f"The matching {subject} has no citation.", element=element, resource_id=resource_id
        )
    evidence_ids = [*citations, *extra_evidence_ids]
    if allowed:
        return allow(evidence_ids)
    return deny(
        [
            blocker(
                NetworkBlockerCode.DENIED,
                f"The matching {subject} denies this traffic.",
                element=element,
                resource_id=resource_id,
                evidence_ids=evidence_ids,
            )
        ]
    )


def cited_target(
    index: GraphIndex, source_id: str, kind: AttachmentKind, citations: Collection[str]
) -> Resource | None:
    """The stored target of the one `kind` edge citing the same source entry, else None.

    Matching by citation keeps apart entries that share a target form, such as two routes or
    rules naming one prefix list. External and unresolved targets are not stored, so None.
    """
    edges = [
        edge
        for edge in index.outgoing_edges(source_id, attachment_kind=kind)
        if set(edge.evidence_ids) & set(citations)
    ]
    return index.get_resource(edges[0].target_id) if len(edges) == 1 else None


def require_recorded[R: Resource](
    index: GraphIndex, resource_id: str, expected: type[R] | tuple[type[R], ...], label: str
) -> R | NetworkResult:
    """The recorded resource a primitive evaluates, or the unknown result that ends it.

    Non-AWS snapshots give the shared unsupported result; an unrecorded or wrongly typed
    resource is missing evidence.
    """
    if (unsupported_platform := require_aws(index)) is not None:
        return unsupported_platform
    resource = index.get_resource(resource_id)
    if not isinstance(resource, expected):
        return missing(f"The {label} is not recorded.", resource_id=resource_id)
    return resource
