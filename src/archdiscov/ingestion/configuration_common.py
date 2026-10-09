from dataclasses import dataclass

from ..models.architecture import Coverage, Evidence, Resource


@dataclass
class ServiceCollection:
    resources: list[Resource]
    evidence: dict[str, Evidence]
    coverage: list[Coverage]
