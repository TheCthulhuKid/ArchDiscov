from dataclasses import dataclass


@dataclass(frozen=True)
class ContainmentEdge:
    """Stored parent-to-child placement, not an evidence-backed attachment."""

    source_id: str
    target_id: str

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return ()
