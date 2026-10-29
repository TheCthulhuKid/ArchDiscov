from types import SimpleNamespace
from uuid import UUID

import pytest

from archdiscov.ingestion.coverage_updates import with_unresolved_value
from archdiscov.ingestion.database.normalization import _Normalizer as RdsNormalizer
from archdiscov.ingestion.elbv2.normalization import _Normalizer as Elbv2Normalizer
from archdiscov.ingestion.functions.collector import _Normalizer as LambdaNormalizer
from archdiscov.ingestion.iam.normalization import _Normalizer as IamNormalizer
from archdiscov.ingestion.queues.collector import _Collector as SqsCollector
from archdiscov.models.architecture import UnresolvedValue
from archdiscov.models.enums import AwsRegion, CoverageStatus
from tests.support.coverage_data import COVERAGE_OPERATION, coverage

CALLERS = [LambdaNormalizer, RdsNormalizer, SqsCollector, IamNormalizer, Elbv2Normalizer]
DEDUPLICATING = [IamNormalizer, Elbv2Normalizer]
PATH = "Result.Value"


def state(outcome):
    return SimpleNamespace(
        coverage={COVERAGE_OPERATION: outcome},
        evidence={},
        region=AwsRegion.US_EAST_1,
        snapshot_id=UUID(int=1),
    )


def issue(caller, target):
    if caller in DEDUPLICATING:
        caller.issue(target, COVERAGE_OPERATION, PATH, "Service-specific limitation", None)
    else:
        caller.issue(target, COVERAGE_OPERATION, PATH, None)


@pytest.mark.parametrize("caller", CALLERS)
def test_caller_marks_coverage_incomplete_with_its_duplicate_policy(caller):
    original = coverage()
    target = state(original)
    issue(caller, target)
    issue(caller, target)
    updated = target.coverage[COVERAGE_OPERATION]
    assert updated.status == CoverageStatus.INCOMPLETE
    assert updated.result_count == original.result_count
    assert len(updated.unresolved_values) == (1 if caller in DEDUPLICATING else 2)
    assert original.unresolved_values == []


def test_helper_deduplicates_whole_values_and_keeps_a_failure_status():
    original = coverage()
    first = UnresolvedValue(configuration_path=PATH, reason="First limitation")
    other = UnresolvedValue(configuration_path=PATH, reason="Other limitation")
    updated = with_unresolved_value(original, first, deduplicate=True)
    assert updated.status == CoverageStatus.INCOMPLETE
    assert with_unresolved_value(updated, first.model_copy(), deduplicate=True) is updated
    assert with_unresolved_value(updated, other, deduplicate=True).unresolved_values == [
        first,
        other,
    ]
    assert with_unresolved_value(updated, first).unresolved_values == [first, first]
    failed = with_unresolved_value(coverage(CoverageStatus.FAILED), first)
    assert failed.status == CoverageStatus.FAILED
    assert original.unresolved_values == []
