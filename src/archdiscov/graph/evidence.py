from collections.abc import Mapping

from archdiscov.models.cloud_contracts import CloudEvidence
from archdiscov.models.resource_base import Resource


class EvidenceIndex:
    """Index owned evidence and explicit fact citations without inferring provenance."""

    def __init__(
        self, evidence: Mapping[str, CloudEvidence], resources: Mapping[str, Resource]
    ) -> None:
        self._evidence = dict(evidence)
        self.ids = tuple(sorted(evidence))
        owners: dict[str, list[str]] = {}
        for evidence_id in self.ids:
            owners.setdefault(evidence[evidence_id].resource_id, []).append(evidence_id)
        self._owners = {owner: tuple(ids) for owner, ids in owners.items()}
        self._facts = {
            (resource_id, pointer): tuple(ids)
            for resource_id, resource in resources.items()
            for pointer, ids in resource.fact_evidence.items()
        }

    def get(self, evidence_id: str) -> CloudEvidence | None:
        item = self._evidence.get(evidence_id)
        return item.model_copy(deep=True) if item is not None else None

    def owner_ids(self, resource_id: str) -> tuple[str, ...]:
        return self._owners.get(resource_id, ())

    def fact_ids(self, resource_id: str, pointer: str) -> tuple[str, ...]:
        return self._facts.get((resource_id, pointer), ())

    def records(self, ids: tuple[str, ...]) -> tuple[CloudEvidence, ...]:
        return tuple(self._evidence[key].model_copy(deep=True) for key in ids)
