"""Validated coverage updates; callers retain service-specific limitation policies."""

from ..models.architecture import Coverage, UnresolvedValue
from ..models.enums import CoverageStatus


def with_unresolved_value(
    coverage: Coverage, unresolved: UnresolvedValue, *, deduplicate: bool = False
) -> Coverage:
    """Append a limitation without mutating coverage or replacing an existing failure status."""
    if deduplicate and unresolved in coverage.unresolved_values:
        return coverage
    fields = coverage.model_dump()
    fields["unresolved_values"] = [*coverage.unresolved_values, unresolved]
    if coverage.status == CoverageStatus.SUCCESS:
        fields["status"] = CoverageStatus.INCOMPLETE
    return Coverage.model_validate(fields)
