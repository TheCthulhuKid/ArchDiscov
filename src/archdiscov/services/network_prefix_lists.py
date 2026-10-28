"""Managed prefix lists named by security-group rules and routes, resolved to their entries.

A rule or route reaches its list through the `prefix_list_reference` edge citing the same
source entry. Only a stored list with recorded entries can be decided; a list that is not
stored, is external, or has unrecorded entries (an AWS-managed list, a list over the entry
limit, or an import with separate entry resources) is undecided and named.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from archdiscov.models.enums import AttachmentKind
from archdiscov.models.resources.network import ManagedPrefixList

from .network_matching import IpNetwork, Undecided, cited_target

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

PREFIX_LIST_OPERATIONS = frozenset({"DescribeManagedPrefixLists", "GetManagedPrefixListEntries"})

# Each recorded entry's CIDR with the evidence that cites it.
type CitedEntries = tuple[tuple[IpNetwork, tuple[str, ...]], ...]


def resolve_prefix_list(
    index: GraphIndex, source_id: str, citations: Collection[str], prefix_list_id: str
) -> CitedEntries | Undecided:
    """The recorded entries of the list a rule or route names, or why they are unknown."""
    prefix_list = cited_target(index, source_id, AttachmentKind.PREFIX_LIST_REFERENCE, citations)
    if not isinstance(prefix_list, ManagedPrefixList):
        return Undecided.missing(f"Prefix list {prefix_list_id} is not recorded.")
    if prefix_list.entries is None:
        return Undecided.missing(f"The entries of prefix list {prefix_list_id} are not recorded.")
    return tuple(
        (entry.cidr, tuple(prefix_list.fact_evidence.get(f"/entries/{n}", [])))
        for n, entry in enumerate(prefix_list.entries)
    )
