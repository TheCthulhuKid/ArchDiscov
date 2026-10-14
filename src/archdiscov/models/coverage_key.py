from dataclasses import dataclass
from typing import Protocol

from .cloud_context import canonical_service_key
from .cloud_scope import SupportedScope
from .enums import CloudPlatform


class _CoverageContext(Protocol):
    scope: SupportedScope
    location: str | None
    service: str
    operation: str


@dataclass(frozen=True)
class CoverageKey:
    """Immutable operation identity, including the complete cloud scope."""

    platform: CloudPlatform
    scope: tuple[tuple[str, str | None], ...]
    location: str | None
    service: str
    operation: str

    def __post_init__(self) -> None:
        if not self.service.strip() or not self.operation.strip():
            raise ValueError("coverage matching requires a service and operation")
        object.__setattr__(self, "scope", tuple(sorted(self.scope, key=lambda item: item[0])))
        object.__setattr__(self, "service", canonical_service_key(self.platform, self.service))

    @classmethod
    def from_values(
        cls, scope: SupportedScope, location: str | None, service: str, operation: str
    ) -> "CoverageKey":
        return cls(
            platform=scope.platform,
            scope=tuple(scope.model_dump(mode="json", exclude={"platform"}).items()),
            location=location,
            service=service,
            operation=operation,
        )

    @classmethod
    def from_record(cls, record: _CoverageContext) -> "CoverageKey":
        return cls.from_values(record.scope, record.location, record.service, record.operation)
