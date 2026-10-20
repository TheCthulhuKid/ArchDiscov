"""Composed network path from a candidate source to the database, element by element.

Step 29's primitives are composed explicitly: the source's security-group egress, the
database's security-group ingress, the NACLs of both subnets (request and return path),
and the local routes. Elements are required together (`all_of`). The source's interfaces and
addresses are alternatives, so the best is reported (an allow, else an unknown, else a
deny). The database sits in one of its subnets, which is not recorded, so every possible
subnet must work: the worst case over them decides. The database address is bounded by its
subnet CIDR; a source without a recorded address is bounded by its subnet.
The return path is checked on one representative ephemeral port. Statefulness of security
groups is not modelled. This is network permission, never authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import TYPE_CHECKING

from archdiscov.models.analysis.database_access import AccessElementDecision
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.enums import (
    AccessElement,
    NetworkDecision,
    RouteReach,
    RuleDirection,
)
from archdiscov.models.network_path import NetworkResult, compose_decisions

from .database_placement import DatabasePlacement
from .database_sources import CandidateSource, SourceInterface, SourceStatus
from .element_results import blocker_limitations, element_decisions, result_evidence
from .network_composition import decision_rank, groups_allow, nacl_round_trip, subnet_network
from .network_decisions import all_of, missing
from .network_probes import PROTOCOL
from .network_routes import evaluate_subnet_route
from .network_security_groups import Peer

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_INGRESS, _EGRESS = RuleDirection.INGRESS, RuleDirection.EGRESS


@dataclass(frozen=True)
class PathEvaluation:
    """The best alternative's element decisions, composed decision, citations, and blockers."""

    decision: NetworkDecision
    elements: tuple[AccessElementDecision, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]


@dataclass(frozen=True)
class _End:
    """One end of a path: a subnet, the network bounding its address, and its groups."""

    subnet_id: str | None
    network: IPv4Network | None
    group_ids: tuple[str, ...]
    exact: bool = False  # the network is one recorded address, not a bounding subnet

    @property
    def cidr(self) -> str | None:
        return str(self.network) if self.network is not None else None

    def peer(self) -> Peer:
        return Peer(self.network, self.group_ids, bounded=not self.exact)


def _database_ends(index: GraphIndex, placement: DatabasePlacement) -> list[_End]:
    groups = placement.security_group_ids
    if not placement.subnet_ids:
        return [_End(None, None, groups)]
    return [_End(s, subnet_network(index, s), groups) for s in placement.subnet_ids]


def _source_ends(index: GraphIndex, interface: SourceInterface) -> list[_End]:
    groups = interface.security_group_ids
    if interface.addresses:
        return [
            _End(interface.subnet_id, IPv4Network(address), groups, exact=True)
            for address in interface.addresses
        ]
    return [_End(interface.subnet_id, subnet_network(index, interface.subnet_id), groups)]


def _needs(message: str) -> NetworkResult:
    return missing(message, element="database_access")


def _security_groups(
    index: GraphIndex, own: _End, other: _End, direction: RuleDirection, port: int
) -> NetworkResult:
    """The traffic must be allowed by some security group of the end that owns the rules."""
    if not own.group_ids:
        return _needs("No security group is recorded for this end of the path.")
    return groups_allow(index, own.group_ids, direction, other.peer(), protocol=PROTOCOL, port=port)


def _nacls(
    index: GraphIndex, own: _End, other: _End, first: RuleDirection, port: int
) -> NetworkResult:
    """Request and reply through the subnet's ACL (the request direction is `first`)."""
    if own.subnet_id is None or other.cidr is None:
        return _needs("The subnet or the other end's address range is not recorded.")
    return nacl_round_trip(index, own.subnet_id, first, other.cidr, protocol=PROTOCOL, port=port)


def _routes(index: GraphIndex, source: _End, database: _End) -> NetworkResult:
    """Local routes in both directions."""
    if None in (source.subnet_id, database.subnet_id, source.cidr, database.cidr):
        return _needs("A subnet or address range needed to select routes is not recorded.")
    return all_of(
        evaluate_subnet_route(index, a.subnet_id, b.cidr, reach=RouteReach.LOCAL)
        for a, b in ((source, database), (database, source))
    )


def _path(
    index: GraphIndex, source: _End, database: _End, port: int
) -> dict[AccessElement, NetworkResult]:
    elements = {
        AccessElement.SOURCE_EGRESS: _security_groups(index, source, database, _EGRESS, port),
        AccessElement.DATABASE_INGRESS: _security_groups(index, database, source, _INGRESS, port),
    }
    if source.subnet_id is None or source.subnet_id != database.subnet_id:
        # Traffic inside one subnet is not filtered by its network ACL.
        elements[AccessElement.SOURCE_NACL] = _nacls(index, source, database, _EGRESS, port)
        elements[AccessElement.DATABASE_NACL] = _nacls(index, database, source, _INGRESS, port)
    elements[AccessElement.ROUTE] = _routes(index, source, database)
    return elements


def _rank(path: dict[AccessElement, NetworkResult]) -> int:
    return decision_rank(path.values())


def _unplaced() -> dict[AccessElement, NetworkResult]:
    return {
        element: _needs("The source's network placement is not established.")
        for element in AccessElement
    }


def _alternatives(
    index: GraphIndex, placement: DatabasePlacement, candidate: CandidateSource, port: int
) -> list[dict[AccessElement, NetworkResult]]:
    """One path per source end: the worst case over where the database may sit."""
    if candidate.status == SourceStatus.INCOMPLETE:
        return [_unplaced()]
    databases = _database_ends(index, placement)
    return [
        max((_path(index, source, database, port) for database in databases), key=_rank)
        for item in candidate.interfaces
        for source in _source_ends(index, item)
    ]


def evaluate_candidate(
    index: GraphIndex, placement: DatabasePlacement, candidate: CandidateSource, port: int
) -> PathEvaluation:
    """The best source alternative's composed path to the database on the port."""
    best = min(_alternatives(index, placement, candidate, port), key=_rank)
    results = [best[element] for element in AccessElement if element in best]
    return PathEvaluation(
        decision=compose_decisions(result.decision for result in results),
        elements=element_decisions(AccessElementDecision, best, list(AccessElement)),
        evidence_ids=tuple(
            sorted(
                {
                    *candidate.evidence_ids,
                    *placement.evidence_ids,
                    *result_evidence(results),
                }
            )
        ),
        limitations=(*candidate.limitations, *blocker_limitations(results)),
    )
