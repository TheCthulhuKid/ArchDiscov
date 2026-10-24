"""Compare two explicitly selected snapshots into one deterministic result.

Matching, field comparison, and qualification are composed here. The comparison is bounded:
each kind of entry (and each side's unmatched resources) keeps its first `result_limit`
resources in ID order, and a `result_limit_reached` limitation reports that more exist. Neither
snapshot is changed, and the result describes recorded data only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from archdiscov.graph import GraphIndex
from archdiscov.models.comparison.entries import (
    AmbiguousMatch,
    MatchedPair,
    UnmatchedResource,
)
from archdiscov.models.comparison.results import (
    DEFAULT_RESULT_LIMIT,
    MAX_RESULT_LIMIT,
    ComparisonResult,
)
from archdiscov.models.enums import ComparisonLimitationKind, ComparisonSide
from archdiscov.models.snapshot_access import SupportedSnapshot

from .comparison_fields import FieldComparer
from .comparison_matching import match_resources
from .comparison_qualification import EntryBuilder, comparison_snapshot, snapshot_limitations


def compare_indexes(
    left: GraphIndex,
    right: GraphIndex,
    *,
    result_limit: int = DEFAULT_RESULT_LIMIT,
    now: datetime | None = None,
) -> ComparisonResult:
    """Compare two indexes; `now` is the creation time (default: the current time)."""
    if not 1 <= result_limit <= MAX_RESULT_LIMIT:
        raise ValueError(f"result_limit must be between 1 and {MAX_RESULT_LIMIT}")
    outcome = match_resources(left, right)
    builder = EntryBuilder(left, right)
    comparer = FieldComparer(left, right, outcome.matched)
    matched = [
        builder.matched(pair, comparer.compare(pair)) for pair in outcome.matched[:result_limit]
    ]
    unmatched = [
        builder.unmatched(side, resource_id)
        for side, ids in (
            (ComparisonSide.LEFT, outcome.unmatched_left),
            (ComparisonSide.RIGHT, outcome.unmatched_right),
        )
        for resource_id in ids[:result_limit]
    ]
    ambiguous = [builder.ambiguous(group) for group in outcome.ambiguous[:result_limit]]
    reasons = list(outcome.limitations)
    if any(
        len(items) > result_limit
        for items in (
            outcome.matched,
            outcome.unmatched_left,
            outcome.unmatched_right,
            outcome.ambiguous,
        )
    ):
        reasons.append(ComparisonLimitationKind.RESULT_LIMIT_REACHED)
    return ComparisonResult(
        left=comparison_snapshot(ComparisonSide.LEFT, left),
        right=comparison_snapshot(ComparisonSide.RIGHT, right),
        created_at=now or datetime.now(UTC),
        result_limit=result_limit,
        matched=_by_id(matched),
        unmatched=_by_id(unmatched),
        ambiguous=_by_id(ambiguous),
        limitations=snapshot_limitations(left, right, reasons),
    )


def compare_snapshots(
    left: SupportedSnapshot,
    right: SupportedSnapshot,
    *,
    result_limit: int = DEFAULT_RESULT_LIMIT,
    now: datetime | None = None,
) -> ComparisonResult:
    """Compare two validated snapshots through one read-only index each."""
    return compare_indexes(GraphIndex(left), GraphIndex(right), result_limit=result_limit, now=now)


def _by_id[E: (MatchedPair, UnmatchedResource, AmbiguousMatch)](entries: list[E]) -> list[E]:
    return sorted(entries, key=lambda entry: entry.id)
