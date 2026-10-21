"""The per-run record of what tools delivered to the model.

Step 37 accepts an answer only if each finding, evidence, and resource ID it cites was
returned by a tool in the same run, for the same snapshot, and checks its status and
limitations against the findings themselves. The record therefore keeps:

- every ID in a delivered output, found by field name (`ID_FIELDS`; a value may be one ID,
  a sequence, or a `BoundedList` of IDs) and scoped to the nearest enclosing `snapshot_id`;
- a view of each delivered analyzer finding;
- the source kind and collection window of each session snapshot;
- a fingerprint (normalized length and SHA-256) of every `untrusted_text` value delivered,
  so narrative can be checked for repeated imported text without keeping that text.

Each delivered output is collected in full before anything is committed. `returned()` and
`context()` are sorted, frozen, and detached from later recording or caller changes.
"""

import hashlib
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, NonNegativeInt, model_validator

from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerKind,
    NetworkDecision,
    SnapshotSourceKind,
)
from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.validation_errors import validation_error

from ..conversation import Contract
from .contracts import ID_FIELDS, BoundedList, DeliveredAnalysis, IdKind, ToolOutput, UntrustedText

_NETWORK_BLOCKS = ("database_access", "internet_exposure")


def _sorted_unique(values: Iterable) -> bool:
    values = list(values)
    return values == sorted(set(values))


class SnapshotReturnedIds(Contract):
    snapshot_id: UUID
    finding_ids: tuple[NonEmptyString, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...] = ()
    resource_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def require_sorted_unique(self) -> Self:
        for ids in (self.finding_ids, self.evidence_ids, self.resource_ids):
            if not _sorted_unique(ids):
                raise validation_error("tool_returned_ids_order", "sorted and unique")
        return self


class ReturnedIds(Contract):
    """Every ID returned in a run, per snapshot, in sorted order."""

    snapshots: tuple[SnapshotReturnedIds, ...] = ()

    @model_validator(mode="after")
    def require_sorted_snapshots(self) -> Self:
        if not _sorted_unique(entry.snapshot_id for entry in self.snapshots):
            raise validation_error("tool_returned_ids_order", "sorted and unique snapshots")
        return self

    def contains(self, kind: IdKind, snapshot_id: UUID, value: str) -> bool:
        for entry in self.snapshots:
            if entry.snapshot_id == snapshot_id:
                return value in getattr(entry, f"{IdKind(kind).value}_ids")
        return False


class RecordedFinding(Contract):
    """What validation needs from one delivered finding."""

    snapshot_id: UUID
    finding_id: NonEmptyString
    analyzer: AnalyzerKind
    result_keys: tuple[NonEmptyString, ...]
    status: AnalysisStatus
    limitation_codes: tuple[NonEmptyString, ...]
    evidence_ids: tuple[NonEmptyString, ...]
    resource_ids: tuple[NonEmptyString, ...]
    primary_resource_id: NonEmptyString | None
    decision: NetworkDecision | None

    @model_validator(mode="after")
    def require_sorted_unique(self) -> Self:
        for values in (
            self.result_keys,
            self.limitation_codes,
            self.evidence_ids,
            self.resource_ids,
        ):
            if not _sorted_unique(values):
                raise validation_error("tool_returned_ids_order", "sorted and unique")
        return self


class RecordedSnapshot(Contract):
    snapshot_id: UUID
    source_kind: SnapshotSourceKind
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime


class TextFingerprint(Contract):
    """A delivered `untrusted_text` value, as its normalized length and SHA-256."""

    length: NonNegativeInt
    digest: str


def normalize_text(text: str) -> str:
    """Case-folded with whitespace runs collapsed, as fingerprints compare text."""
    return " ".join(text.casefold().split())


def fingerprint(text: str) -> TextFingerprint:
    normalized = normalize_text(text)
    return TextFingerprint(
        length=len(normalized), digest=hashlib.sha256(normalized.encode()).hexdigest()
    )


class RunContext(Contract):
    """Everything a proposal is validated against, in sorted order."""

    returned: ReturnedIds
    snapshots: tuple[RecordedSnapshot, ...]
    findings: tuple[RecordedFinding, ...]
    fingerprints: tuple[TextFingerprint, ...]

    def finding(self, snapshot_id: UUID, finding_id: str) -> RecordedFinding | None:
        for item in self.findings:
            if (item.snapshot_id, item.finding_id) == (snapshot_id, finding_id):
                return item
        return None

    def snapshot(self, snapshot_id: UUID) -> RecordedSnapshot | None:
        return next((item for item in self.snapshots if item.snapshot_id == snapshot_id), None)


def _finding_references(finding: AnalysisFinding) -> tuple[set[str], set[str]]:
    """Evidence and resource IDs anywhere in a finding."""
    evidence = set(finding.evidence_ids)
    resources = {reference.resource_id for reference in finding.resources}
    for step in finding.path:
        evidence.update(step.evidence_ids)
        resources.add(step.source_id)
        if step.kind == "containment":
            resources.add(step.target_id)
        elif isinstance(step.target, LocalTarget):
            resources.add(step.target.resource_id)
    for limitation in finding.limitations:
        evidence.update(limitation.evidence_ids)
        if limitation.unresolved_target is not None:
            evidence.update(limitation.unresolved_target.evidence_ids)
            resources.add(limitation.unresolved_target.source_id)
    return evidence, resources


def _decision(finding: AnalysisFinding) -> NetworkDecision | None:
    for block in _NETWORK_BLOCKS:
        if (value := getattr(finding, block)) is not None:
            return value.decision
    return None


class _Collected:
    """One output's contribution, gathered before anything is committed."""

    def __init__(self) -> None:
        self.ids: list[tuple[UUID, IdKind, str]] = []
        self.fingerprints: set[TextFingerprint] = set()


class RunRecord:
    """Accumulates what delivered outputs returned; reads are immutable sorted copies."""

    def __init__(self) -> None:
        self._ids: dict[UUID, dict[IdKind, set[str]]] = {}
        self._snapshots: dict[UUID, RecordedSnapshot] = {}
        self._findings: dict[tuple[UUID, str], RecordedFinding] = {}
        self._fingerprints: set[TextFingerprint] = set()

    def record(self, snapshot_id: UUID, kind: IdKind, ids: Iterable[str]) -> None:
        for value in _checked(ids):
            self._add(snapshot_id, kind, value)

    def _add(self, snapshot_id: UUID, kind: IdKind, value: str) -> None:
        bucket = self._ids.setdefault(UUID(str(snapshot_id)), {item: set() for item in IdKind})
        bucket[IdKind(kind)].add(value)

    def record_snapshot(
        self,
        snapshot_id: UUID,
        source_kind: SnapshotSourceKind,
        collection_window: tuple[datetime, datetime],
    ) -> None:
        started, completed = collection_window
        self._snapshots[snapshot_id] = RecordedSnapshot(
            snapshot_id=snapshot_id,
            source_kind=source_kind,
            collection_started_at=started,
            collection_completed_at=completed,
        )

    def record_output(self, output: ToolOutput) -> None:
        """Record a delivered output; unscoped or blank IDs are an error, and nothing is kept."""
        collected = _Collected()
        _collect(output, None, collected)
        _checked(value for _, _, value in collected.ids)
        analysis = output.delivered_analysis()
        findings = [] if analysis is None else _delivered_findings(analysis)
        for snapshot_id, kind, value in collected.ids:
            self._add(snapshot_id, kind, value)
        self._fingerprints |= collected.fingerprints
        for finding in findings:
            self._merge(finding)

    def _merge(self, finding: RecordedFinding) -> None:
        """Keep one view per finding, listing every result it was delivered from."""
        key = (finding.snapshot_id, finding.finding_id)
        existing = self._findings.get(key)
        if existing is not None:
            keys = sorted({*existing.result_keys, *finding.result_keys})
            finding = existing.model_copy(update={"result_keys": tuple(keys)})
        self._findings[key] = finding

    def returned(self) -> ReturnedIds:
        return ReturnedIds(
            snapshots=tuple(
                SnapshotReturnedIds(
                    snapshot_id=snapshot_id,
                    finding_ids=tuple(sorted(ids[IdKind.FINDING])),
                    evidence_ids=tuple(sorted(ids[IdKind.EVIDENCE])),
                    resource_ids=tuple(sorted(ids[IdKind.RESOURCE])),
                )
                for snapshot_id, ids in sorted(self._ids.items())
            )
        )

    def context(self) -> RunContext:
        return RunContext(
            returned=self.returned(),
            snapshots=tuple(self._snapshots[key] for key in sorted(self._snapshots)),
            findings=tuple(self._findings[key] for key in sorted(self._findings)),
            fingerprints=tuple(
                sorted(self._fingerprints, key=lambda item: (item.length, item.digest))
            ),
        )


def _checked(ids: Iterable[str]) -> list[str]:
    values = [ids] if isinstance(ids, str) else list(ids)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("IDs must be non-blank strings")
    return values


def _delivered_findings(analysis: DeliveredAnalysis) -> list[RecordedFinding]:
    result = analysis.result
    delivered = set(analysis.finding_ids)
    found = []
    for finding in result.findings:
        if finding.id not in delivered:
            continue
        evidence, resources = _finding_references(finding)
        found.append(
            RecordedFinding(
                snapshot_id=finding.snapshot_id,
                finding_id=finding.id,
                analyzer=result.analyzer,
                result_keys=(analysis.result_key,),
                status=finding.status,
                limitation_codes=tuple(sorted({item.code for item in finding.limitations})),
                evidence_ids=tuple(sorted(evidence)),
                resource_ids=tuple(sorted(resources)),
                primary_resource_id=finding.resources[0].resource_id if finding.resources else None,
                decision=_decision(finding),
            )
        )
    if len(found) != len(delivered):
        raise ValueError("delivered finding IDs must belong to the delivered result")
    return found


def _collect(value: Any, snapshot_id: UUID | None, collected: _Collected) -> None:
    if isinstance(value, UntrustedText):
        collected.fingerprints.add(fingerprint(value.untrusted_text))
    elif isinstance(value, BaseModel):
        fields = type(value).model_fields
        if "snapshot_id" in fields and getattr(value, "snapshot_id") is not None:
            snapshot_id = getattr(value, "snapshot_id")
        for name in fields:
            item = getattr(value, name)
            if name not in ID_FIELDS:
                _collect(item, snapshot_id, collected)
                continue
            if item is None:
                continue
            if snapshot_id is None:
                raise ValueError(f"{type(value).__name__}.{name} has no enclosing snapshot_id")
            if isinstance(item, BoundedList):
                item = item.items
            for entry in [item] if isinstance(item, str) else item:
                collected.ids.append((snapshot_id, ID_FIELDS[name], entry))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, snapshot_id, collected)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, snapshot_id, collected)
