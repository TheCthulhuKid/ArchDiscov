from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from archdiscov.models.architecture import UnresolvedTarget
from archdiscov.models.cloud_context import validate_context
from archdiscov.models.cloud_contracts import CloudExternalTarget
from archdiscov.models.cloud_scope import SupportedScope
from archdiscov.models.enums import CoverageStatus, QueryLimitationKind
from archdiscov.models.resource_base import FactPath, Model, NonEmptyString


class QueryCoverageReference(Model):
    """Identify an operation's complete cloud context without asserting it exists.

    A null location means scope-wide placement, not an unknown region.
    """

    scope: SupportedScope
    location: NonEmptyString | None = None
    service: NonEmptyString
    operation: NonEmptyString

    @model_validator(mode="after")
    def validate_scope_context(self) -> Self:
        validate_context(self.scope, self.location, self.service)
        return self


class QueryMissingCoverageLimitation(Model):
    """Describe an absent operation record, which has no recorded outcome."""

    kind: Literal[QueryLimitationKind.COVERAGE_MISSING] = QueryLimitationKind.COVERAGE_MISSING
    coverage: QueryCoverageReference
    message: NonEmptyString


class QueryLimitedCoverageLimitation(Model):
    """Preserve a recorded limited outcome; successful-zero is not a limitation.

    The query service checks that the status matches the referenced record.
    """

    kind: Literal[QueryLimitationKind.COVERAGE_LIMITED] = QueryLimitationKind.COVERAGE_LIMITED
    coverage: QueryCoverageReference
    status: Literal[
        CoverageStatus.FAILED,
        CoverageStatus.INCOMPLETE,
        CoverageStatus.SKIPPED,
        CoverageStatus.UNSUPPORTED,
    ]
    message: NonEmptyString


class QueryProvenanceLimitation(Model):
    """Identify unavailable fact provenance without asserting the fact's absence.

    The resource-relative pointer is syntax-validated but need not resolve to
    a recorded field, unlike a fact citation.
    """

    kind: Literal[QueryLimitationKind.PROVENANCE_UNAVAILABLE] = (
        QueryLimitationKind.PROVENANCE_UNAVAILABLE
    )
    resource_id: NonEmptyString
    fact_path: FactPath
    message: NonEmptyString


class QueryExternalTargetLimitation(Model):
    """Retain an external terminal and originating edge without inventing a node.

    Target scope is recorded independently; unknown context must not be filled
    from the source scope. Graph identity consistency is checked by services.
    """

    kind: Literal[QueryLimitationKind.EXTERNAL_TARGET] = QueryLimitationKind.EXTERNAL_TARGET
    terminal_id: NonEmptyString
    edge_id: NonEmptyString
    target: CloudExternalTarget
    message: NonEmptyString


class QueryUnresolvedTargetLimitation(Model):
    """Retain a terminal's recorded reason and source path without guessing an ID.

    Terminal and originating-edge consistency is checked by the query service.
    """

    kind: Literal[QueryLimitationKind.UNRESOLVED_TARGET] = QueryLimitationKind.UNRESOLVED_TARGET
    terminal_id: NonEmptyString
    edge_id: NonEmptyString
    target: UnresolvedTarget
    message: NonEmptyString


QueryLimitation = Annotated[
    QueryMissingCoverageLimitation
    | QueryLimitedCoverageLimitation
    | QueryProvenanceLimitation
    | QueryExternalTargetLimitation
    | QueryUnresolvedTargetLimitation,
    Field(discriminator="kind"),
]
