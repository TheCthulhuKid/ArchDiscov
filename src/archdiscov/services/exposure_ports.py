"""Ingress port ranges that recorded security-group rules open to the whole internet.

Only an IPv4 rule for `0.0.0.0/0` admits the internet. Rules naming only referenced groups,
prefix lists, or IPv6 never create an exposure here; they are reported as unsupported so the
result says what was not considered. Ranges are merged per protocol, deterministically, and
can be split at the boundaries other rules and ACL entries introduce so each piece is
decided uniformly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network

from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.enums import ExposureProtocol
from archdiscov.models.resources.network import NaclEntry, SecurityGroup, SecurityGroupRule

from .network_matching import ALL_PROTOCOLS, normalize_protocol, omitted_rules
from .placement_collection import limitation

MIN_PORT, MAX_PORT = 1, 65535
UNSUPPORTED_RULE_CODE = "exposure_rule_unsupported"
_IPV6 = "IPv6 rules are unsupported and not considered."
_PREFIX_LIST = "Prefix-list rules are unsupported and not considered."
_GROUP_REFERENCE = "Rules naming a referenced group are not considered for internet exposure."
_INFORMATIONAL = {_GROUP_REFERENCE}
_PROTOCOLS = {"tcp": ExposureProtocol.TCP, "udp": ExposureProtocol.UDP}


@dataclass(frozen=True, order=True)
class PortRange:
    protocol: ExposureProtocol
    start: int
    end: int


@dataclass(frozen=True)
class OpenPorts:
    """Merged internet-open ranges, the rule citations behind them, and what was skipped."""

    ranges: tuple[PortRange, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]
    uncertain: bool  # a rule that might admit the internet could not be interpreted


def _protocols(rule_protocol: str | None) -> list[ExposureProtocol] | None:
    """The exposure protocols a rule covers: [] for icmp, None when unrecognized."""
    if rule_protocol == ALL_PROTOCOLS:
        return list(ExposureProtocol)
    normalized = normalize_protocol(rule_protocol)
    if normalized is None:
        return None
    return [_PROTOCOLS[normalized]] if normalized in _PROTOCOLS else []


def _ports(rule: SecurityGroupRule | NaclEntry) -> tuple[int, int] | None:
    """The inclusive port span of a rule, clamped to 1-65535; None when not recorded."""
    if rule.protocol == ALL_PROTOCOLS:
        return MIN_PORT, MAX_PORT
    if rule.from_port is None or rule.to_port is None:
        return None
    if rule.from_port == -1:
        return MIN_PORT, MAX_PORT
    return max(MIN_PORT, rule.from_port), min(MAX_PORT, rule.to_port)


def _merge(ranges: Iterable[PortRange]) -> list[PortRange]:
    merged: list[PortRange] = []
    for item in sorted(ranges):
        last = merged[-1] if merged else None
        if last and last.protocol == item.protocol and item.start <= last.end + 1:
            merged[-1] = PortRange(last.protocol, last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


def _unsupported(message: str) -> AnalysisLimitation:
    return limitation(UNSUPPORTED_RULE_CODE, message)


def _classify(rule: SecurityGroupRule) -> str | None:
    """How a rule's source relates to the internet: 'open', or why it is unsupported."""
    if isinstance(rule.cidr, IPv4Network):
        return "open" if rule.cidr.prefixlen == 0 else None
    if isinstance(rule.cidr, IPv6Network):
        return (
            "IPv6 rules are unsupported and not considered." if rule.cidr.prefixlen == 0 else None
        )
    if rule.prefix_list_id is not None:
        return _PREFIX_LIST
    if rule.referenced_group_id is not None:
        return _GROUP_REFERENCE
    return None


def internet_open_ports(groups: Iterable[SecurityGroup]) -> OpenPorts:
    """The merged tcp and udp ranges the groups' ingress rules open to `0.0.0.0/0`."""
    found: list[PortRange] = []
    evidence: set[str] = set()
    notes: dict[str, AnalysisLimitation] = {}
    for group in groups:
        if omitted_rules(group):
            notes["omitted"] = limitation(
                "exposure_rules_omitted", "Some security-group rules could not be recorded."
            )
        for position, rule in enumerate(group.ingress_rules):
            kind = _classify(rule)
            if kind is None:
                continue
            if kind != "open":
                notes[kind] = _unsupported(kind)
                continue
            protocols, span = _protocols(rule.protocol), _ports(rule)
            if protocols is None or span is None:
                notes["incomplete"] = _unsupported("A rule open to the internet is incomplete.")
                continue
            found.extend(PortRange(protocol, *span) for protocol in protocols)
            evidence.update(group.fact_evidence.get(f"/ingress_rules/{position}", []))
    return OpenPorts(
        tuple(_merge(found)),
        tuple(sorted(evidence)),
        tuple(notes[key] for key in sorted(notes)),
        uncertain=any(key not in _INFORMATIONAL for key in notes),
    )


def boundaries(
    groups: Iterable[SecurityGroup], entries: Iterable[NaclEntry], protocol: ExposureProtocol
) -> set[int]:
    """Ports where some ingress rule or ACL entry for the protocol starts or stops applying."""
    points: set[int] = set()
    rules = [
        *(rule for group in groups for rule in group.ingress_rules),
        *(entry for entry in entries if entry.direction == "ingress"),
    ]
    for rule in rules:
        protocols, span = _protocols(rule.protocol), _ports(rule)
        if protocols and protocol in protocols and span is not None:
            points.update((span[0], span[1] + 1))
    return points


def split(item: PortRange, points: Iterable[int]) -> list[PortRange]:
    """Cut the range so every piece starts at a boundary: uniform for all rules and entries."""
    cuts = sorted({p for p in points if item.start < p <= item.end})
    starts = [item.start, *cuts]
    ends = [*(c - 1 for c in cuts), item.end]
    return [PortRange(item.protocol, s, e) for s, e in zip(starts, ends, strict=True)]
