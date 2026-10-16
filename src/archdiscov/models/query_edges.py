from typing import Annotated, Literal

from pydantic import Field

from archdiscov.graph.containment import ContainmentEdge
from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.enums import QueryDirection
from archdiscov.models.resource_base import Model


class QueryDependencyEdge(Model):
    """Wrap an original dependency with its identity, target, and citations intact."""

    kind: Literal["dependency"] = "dependency"
    edge: DependencyEdge


class QueryContainmentEdge(Model):
    """Wrap stored parent-to-child structure, legitimately without citations.

    Containment does not assert an attachment or any connectivity semantics.
    """

    kind: Literal["containment"] = "containment"
    edge: ContainmentEdge


QueryEdge = Annotated[QueryDependencyEdge | QueryContainmentEdge, Field(discriminator="kind")]


class QueryTraversalStep(Model):
    """Orient one traversal step without changing its stored edge.

    Outgoing travels source to target; incoming travels target to source while
    retaining the original source's evidence. BOTH is a request option, not an
    individual step orientation. Graph consistency is a service concern.
    """

    edge: QueryEdge
    direction: Literal[
        QueryDirection.OUTGOING,
        QueryDirection.INCOMING,
    ]
