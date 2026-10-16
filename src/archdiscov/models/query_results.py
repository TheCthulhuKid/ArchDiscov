from uuid import UUID

from pydantic import Field

from archdiscov.models.architecture import SupportedResource
from archdiscov.models.cloud_contracts import CloudEvidence, CloudOperationCoverage
from archdiscov.models.cloud_scope import SupportedScope
from archdiscov.models.enums import QueryTruncationReason
from archdiscov.models.query_edges import QueryTraversalStep
from archdiscov.models.query_limitations import QueryLimitation
from archdiscov.models.resource_base import Model, NonEmptyString


class QueryTruncation(Model):
    """Report bounds that prevented further exploration, not merely were reached.

    Empty reasons means no truncation was detected. Services emit unique reasons
    in enum declaration order; multiple bounds can apply. Missing source data is
    a limitation, not truncation. This model does not enforce traversal behaviour.
    """

    reasons: list[QueryTruncationReason] = Field(default_factory=list)


class QuerySearchResult(Model):
    """Return typed recorded matches with snapshot context and uncertainty.

    Services return detached resources in stable-ID order. Empty matches do not
    prove absence; limitations can apply even when no resources are returned.
    """

    snapshot_id: UUID
    scope: SupportedScope
    resources: list[SupportedResource] = Field(default_factory=list)
    limitations: list[QueryLimitation] = Field(default_factory=list)
    truncation: QueryTruncation = Field(default_factory=QueryTruncation)


class QueryInspectResult(Model):
    """Return one typed resource, cited evidence, and relevant recorded coverage.

    Services select relevant records and check snapshot consistency. Coverage
    retains successful-zero outcomes; missing coverage is a separate limitation.
    Unknown stable IDs produce a service error, not an empty resource.
    """

    snapshot_id: UUID
    scope: SupportedScope
    resource: SupportedResource
    evidence: dict[NonEmptyString, CloudEvidence] = Field(default_factory=dict)
    coverage: list[CloudOperationCoverage] = Field(default_factory=list)
    limitations: list[QueryLimitation] = Field(default_factory=list)


class QueryNeighboursResult(Model):
    """Return one oriented step per matching adjacent edge, not multi-edge paths.

    Services check adjacency and preserve parallel occurrences and terminal
    targets. Empty steps mean no recorded matches, not exhaustive absence.

    Services order steps by (edge kind, stored source ID, stored target ID,
    dependency edge ID, direction rank). Containment uses an empty edge ID;
    outgoing ranks before incoming. Apply the result limit after ordering.
    """

    snapshot_id: UUID
    scope: SupportedScope
    resource_id: NonEmptyString
    steps: list[QueryTraversalStep] = Field(default_factory=list)
    limitations: list[QueryLimitation] = Field(default_factory=list)
    truncation: QueryTruncation = Field(default_factory=QueryTruncation)


class QueryPath(Model):
    """Preserve a nonempty ordered path and its path-specific uncertainty.

    Step count gives depth; edges retain their original direction and citations.
    Services ensure continuity, handle cycles, and stop at nonlocal terminals.
    A structural path is not proof of reachability or effective access.
    """

    steps: list[QueryTraversalStep] = Field(min_length=1)
    limitations: list[QueryLimitation] = Field(default_factory=list)


class QueryPathsResult(Model):
    """Return bounded exploration from a stable ID with explicit uncertainty.

    Top-level limitations affect the exploration; individual paths retain their
    own limitations. Truncation describes query bounds rather than source gaps.
    Services check path consistency and return detached, deterministic results.

    Return every matching nonempty path prefix, not only terminal paths.
    Order paths shortest-first, then lexicographically by their sequences
    of neighbour step-order keys. Explore candidates in that same order
    so work-limited results remain deterministic.
    """

    snapshot_id: UUID
    scope: SupportedScope
    resource_id: NonEmptyString
    paths: list[QueryPath] = Field(default_factory=list)
    limitations: list[QueryLimitation] = Field(default_factory=list)
    truncation: QueryTruncation = Field(default_factory=QueryTruncation)
