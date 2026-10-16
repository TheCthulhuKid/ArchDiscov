from pydantic import Field

from archdiscov.models.cloud.scope import SupportedScope
from archdiscov.models.query.parameters import (
    QueryLimitResult,
    QueryPathLimits,
    QueryTraversalOptions,
)
from archdiscov.models.resource_base import Model, NonEmptyString


class QueryNeighboursRequest(Model):
    """Select immediate adjacency by stable ID, not by name or physical ID.

    Resource existence and edge selection are checked by the query service.
    """

    resource_id: NonEmptyString
    options: QueryTraversalOptions = Field(default_factory=QueryTraversalOptions)
    limit_result: QueryLimitResult = 100


class QueryPathsRequest(Model):
    """Request bounded exploration from one stable resource ID.

    This is not a source-to-destination route or a reachability analysis.
    Resource existence is checked by the query service.
    """

    resource_id: NonEmptyString
    options: QueryTraversalOptions = Field(default_factory=QueryTraversalOptions)
    limits: QueryPathLimits = Field(default_factory=QueryPathLimits)


class QueryInspectRequest(Model):
    """Select one stored resource by stable ID; existence is a service concern."""

    resource_id: NonEmptyString


class QueryTagFilter(Model):
    """Match a recorded tag key and optional value exactly and case-sensitively.

    None selects any value for the key; an empty string selects an empty value.
    Unavailable tag data is not proof that a resource has no tags.
    """

    key: NonEmptyString
    value: str | None = None


class QueryPhysicalIdentifierFilter(Model):
    """Qualify a physical ID by cloud scope and provider-native resource type.

    Matching uses the graph's provider-specific identity rules and may retain
    multiple candidates. This model validates shape, not identity existence.
    """

    scope: SupportedScope
    native_type: NonEmptyString
    physical_id: NonEmptyString


class QuerySearchRequest(Model):
    """Search recorded resources using AND-combined optional filters.

    Omitted filters impose no restriction. Names, stable IDs, and tags
    match exactly and case-sensitively. Native types and physical IDs
    follow the graph's provider-specific identity matching rules.

    A tag value of None selects by key alone; an empty string matches
    an explicitly empty value. Qualified physical IDs may match multiple
    resources; ambiguous candidates must not be silently discarded.

    Services order results by stable resource ID before applying the limit.
    No matches means no recorded matches, not proof of absence.
    """

    native_type: NonEmptyString | None = None
    name: NonEmptyString | None = None
    limit_result: QueryLimitResult = 100
    tag: QueryTagFilter | None = None
    resource_id: NonEmptyString | None = None
    physical_identifier: QueryPhysicalIdentifierFilter | None = None
