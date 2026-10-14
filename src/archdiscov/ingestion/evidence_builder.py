"""Shared evidence, citation, and redaction helpers for EC2 normalizers."""

import json
from collections.abc import Collection
from datetime import date
from typing import Any
from uuid import UUID

from ..models.architecture import Evidence, Resource, ResourceIdentity
from ..models.enums import AwsRegion, AwsService, SourceType
from ..models.json_pointer import escape_pointer_token as _escape
from ..models.sanitization import is_sensitive_path

REDACTED = "[REDACTED]"


def _default(value: Any) -> str:
    if isinstance(value, date):  # datetime is a date subclass
        return value.isoformat()
    return "<unserializable>"


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_default))


def redact_tag_pairs(value: Any, path: str = "") -> tuple[Any, list[str]]:
    """Redact the Value of AWS {"Key": ..., "Value": ...} pairs whose Key names a secret.

    Key-name sanitization cannot see these because the secret's name is data, not a field name.
    """
    paths: list[str] = []
    if isinstance(value, dict):
        out = {}
        pair = isinstance(value.get("Key"), str) and "Value" in value
        for key, child in value.items():
            if pair and key == "Value" and is_sensitive_path(value["Key"]):
                out[key] = REDACTED
                paths.append(f"{path}/{_escape(key)}")
                continue
            out[key], child_paths = redact_tag_pairs(child, f"{path}/{_escape(key)}")
            paths += child_paths
        return out, paths
    if isinstance(value, list):
        items = []
        for index, child in enumerate(value):
            redacted, child_paths = redact_tag_pairs(child, f"{path}/{index}")
            items.append(redacted)
            paths += child_paths
        return items, paths
    return value, paths


def redact_named_keys(value: Any, names: Collection[str], path: str = "") -> tuple[Any, list[str]]:
    """Redact every value stored under one of `names`, e.g. free-text rule descriptions."""
    paths: list[str] = []
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key in names and child is not None:
                out[key] = REDACTED
                paths.append(f"{path}/{_escape(key)}")
                continue
            out[key], child_paths = redact_named_keys(child, names, f"{path}/{_escape(key)}")
            paths += child_paths
        return out, paths
    if isinstance(value, list):
        items = []
        for index, child in enumerate(value):
            redacted, child_paths = redact_named_keys(child, names, f"{path}/{index}")
            items.append(redacted)
            paths += child_paths
        return items, paths
    return value, paths


def tags_of(item: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag["Key"]): str(tag.get("Value", ""))
        for tag in item.get("Tags") or []
        if isinstance(tag, dict) and "Key" in tag
    }


class EvidenceBuilder:
    """Creates EC2 resources with whole-item evidence and exact per-fact citations."""

    redact_keys: frozenset[str] = frozenset()

    def __init__(
        self,
        snapshot_id: UUID,
        account_id: str,
        region: AwsRegion,
        service: AwsService = AwsService.EC2,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.account_id = account_id
        self.region = region
        self.service = service
        self.evidence: dict[str, Evidence] = {}

    def _evidence(self, key: str, resource_id: str, operation: str, path: str, value: Any) -> str:
        safe, redacted_paths = redact_tag_pairs(json_safe(value))
        if self.redact_keys:
            safe, more = redact_named_keys(safe, self.redact_keys)
            redacted_paths += more
        self.evidence[key] = Evidence(
            snapshot_id=self.snapshot_id,
            resource_id=resource_id,
            source=SourceType.AWS,
            locator=f"{self.service.value}:{operation}",
            configuration_path=path,
            value=safe,
            redacted_paths=redacted_paths,
        )
        return key

    def source_evidence(self, resource: Resource, operation: str, path: str, value: Any) -> str:
        """Record one source entry as evidence owned by `resource`; returns its evidence ID."""
        prefix = "" if self.service == AwsService.EC2 else f"{self.service.value}/"
        key = f"{prefix}{self.region.value}/{operation}/{resource.identity.physical_id}#{path}"
        return self._evidence(key, resource.id, operation, path, value)

    def cite(self, resource: Resource, pointer: str, operation: str, path: str, value: Any) -> None:
        """Cite one source entry (e.g. a single rule) for a normalized fact pointer."""
        evidence_id = self.source_evidence(resource, operation, path, value)
        resource.fact_evidence.setdefault(pointer, []).append(evidence_id)

    def _build(
        self,
        cls: type[Resource],
        *,
        operation: str,
        item_path: str,
        item: dict[str, Any],
        physical_id: str,
        attributes: dict[str, Any],
        facts: dict[str, tuple[str, Any]],
        **fields: Any,
    ) -> Resource:
        """Create a resource with whole-item evidence and one citation per present fact.

        `facts` maps a normalized resource JSON Pointer to (source field, value).
        """
        identity = ResourceIdentity(
            account_id=self.account_id,
            region=self.region,
            service=self.service,
            physical_id=physical_id,
        )
        rid = identity.stable_id
        prefix = "" if self.service == AwsService.EC2 else f"{self.service.value}/"
        base = f"{prefix}{self.region.value}/{operation}/{physical_id}"
        evidence_ids = [self._evidence(base, rid, operation, item_path, item)]
        fact_evidence: dict[str, list[str]] = {}
        for pointer, (source_field, value) in facts.items():
            key = f"{base}#{source_field}"
            fact_evidence[pointer] = [
                self._evidence(key, rid, operation, f"{item_path}.{source_field}", value)
            ]
        return cls(
            identity=identity,
            attributes=json_safe({k: v for k, v in attributes.items() if v is not None}),
            evidence_ids=evidence_ids,
            fact_evidence=fact_evidence,
            **fields,
        )
