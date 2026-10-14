"""Normalize observed global IAM resources without inventing identities or documents."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ...models.architecture import Coverage, Evidence, Resource, ResourceIdentity, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ...models.iam import (
    IamInlinePolicy,
    IamInstanceProfile,
    IamManagedPolicy,
    IamRole,
    normalize_document,
    sanitize_document,
)
from ...models.json_pointer import escape_pointer_token
from ...models.sanitization import sanitize_value
from ..coverage_updates import with_unresolved_value
from ..evidence_builder import EvidenceBuilder, json_safe, redact_tag_pairs, tags_of
from ..operations import IamOperation as Op
from .collector import IamInventory
from .execution import IamResponse

_DOCUMENT_KEYS = frozenset({"AssumeRolePolicyDocument", "PolicyDocument", "Document"})
_INVALID_DOCUMENT = "Malformed IAM document was omitted."
_MISSING_DOCUMENT = "IAM document was unavailable; no document was substituted."


@dataclass
class IamNormalized:
    resources: list[Resource]
    evidence: dict[str, Evidence]
    coverage: list[Coverage]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def safe_tree(value: Any) -> tuple[Any, list[str], list[str]]:
    """Decode all embedded documents, retaining redaction pointers and invalid source paths."""
    redacted: list[str] = []
    invalid: list[str] = []

    def visit(item: Any, pointer: str, source_path: str) -> Any:
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                escaped = escape_pointer_token(key)
                child_pointer = f"{pointer}/{escaped}"
                child_path = f"{source_path}.{key}"
                if key in _DOCUMENT_KEYS:
                    try:
                        child, paths = sanitize_document(normalize_document(child))
                    except ValueError:
                        invalid.append(child_path)
                        continue
                    redacted.extend(child_pointer + path for path in paths)
                result[key] = visit(child, child_pointer, child_path)
            return result
        if isinstance(item, list):
            return [
                visit(child, f"{pointer}/{index}", f"{source_path}[{index}]")
                for index, child in enumerate(item)
            ]
        return item

    return visit(value, "", ""), sorted(set(redacted)), sorted(set(invalid))


class _Normalizer(EvidenceBuilder):
    def __init__(self, snapshot_id: UUID, account_id: str, inventory: IamInventory) -> None:
        super().__init__(snapshot_id, account_id, AwsRegion.GLOBAL, AwsService.IAM)
        self.inventory = inventory
        self.coverage = {
            op: result.coverage.model_copy(deep=True) for op, result in inventory.results.items()
        }
        self.resources: list[Resource] = []

    def responses(self, op: Op, **params: str) -> list[IamResponse]:
        result = self.inventory.results.get(op)
        if result is None:
            return []
        return sorted(
            (
                response
                for response in result.responses
                if all(response.call.params.get(key) == value for key, value in params.items())
            ),
            key=lambda response: response.path,
        )

    def issue(self, op: Op, path: str, reason: str, resource: Resource | None = None) -> None:
        if resource is not None:
            limitation = f"{op}: {reason}"
            limitations = resource.attributes.setdefault("limitations", [])
            if limitation not in limitations:
                limitations.append(limitation)
        coverage = self.coverage.get(op)
        if coverage is None or coverage.status in (
            CoverageStatus.SKIPPED,
            CoverageStatus.UNSUPPORTED,
        ):
            return
        unresolved = UnresolvedValue(
            resource_id=resource.id if resource is not None else None,
            configuration_path=path,
            reason=reason,
        )
        self.coverage[op] = with_unresolved_value(coverage, unresolved, deduplicate=True)

    def unavailable(self, resource: Resource, op: Op, reason: str) -> None:
        limitation = f"{op}: {reason}"
        limitations = resource.attributes.setdefault("limitations", [])
        if limitation not in limitations:
            limitations.append(limitation)

    def discovery_limit(self, resource: Resource, op: Op) -> None:
        coverage = self.coverage.get(op)
        if coverage is None or coverage.status != CoverageStatus.SUCCESS:
            self.unavailable(resource, op, "Discovery was unavailable or incomplete.")

    def _evidence(self, key: str, resource_id: str, operation: str, path: str, value: Any) -> str:
        if path.rsplit(".", 1)[-1] in _DOCUMENT_KEYS:
            tree, paths, _ = safe_tree({"Document": value})
            safe = tree["Document"]
            redacted = [pointer.removeprefix("/Document") for pointer in paths]
        else:
            safe, redacted, _ = safe_tree(value)
        result = super()._evidence(key, resource_id, operation, path, safe)
        evidence = self.evidence[result]
        self.evidence[result] = Evidence.model_validate(
            {
                **evidence.model_dump(),
                "redacted_paths": sorted(set(evidence.redacted_paths + redacted)),
            }
        )
        return result

    def whole(self, resource: Resource, response: IamResponse) -> Any:
        safe, _, invalid = safe_tree(response.value)
        for suffix in invalid:
            self.issue(response.call.operation, response.path + suffix, _INVALID_DOCUMENT, resource)
        evidence_id = self.source_evidence(
            resource, response.call.operation, response.path, response.value
        )
        if evidence_id not in resource.evidence_ids:
            resource.evidence_ids.append(evidence_id)
        return safe

    def selected(self, op: Op, wrapper: str | None, id_field: str, name_field: str):
        candidates = []
        for response in self.responses(op):
            item = response.value
            if wrapper is not None:
                item = item.get(wrapper) if isinstance(item, dict) else None
            path = response.path + (f".{wrapper}" if wrapper else "")
            if (
                not isinstance(item, dict)
                or not all(_text(item.get(key)) for key in (id_field, "Arn"))
                or item[id_field].startswith("arn:")
            ):
                self.issue(op, path, "Missing or invalid source identity; resource was omitted.")
                continue
            if op == Op.GET_POLICY and item["Arn"] != response.call.params.get("PolicyArn"):
                self.issue(op, path, "Conflicting source identity; resource was omitted.")
                continue
            candidates.append((response, item, path))
        seen = set()
        for response, item, path in candidates:
            name = item.get(name_field) if _text(item.get(name_field)) else None
            signature = (item[id_field], item["Arn"], name)
            identity_fields = (
                (id_field, "Arn") if op == Op.GET_POLICY else (id_field, "Arn", name_field)
            )
            ambiguous = any(
                (
                    other[id_field],
                    other["Arn"],
                    other.get(name_field) if _text(other.get(name_field)) else None,
                )
                != signature
                and any(
                    _text(item.get(key)) and item.get(key) == other.get(key)
                    for key in identity_fields
                )
                for _, other, _ in candidates
            )
            if ambiguous:
                self.issue(op, path, "Ambiguous source identity; resource was omitted.")
                continue
            if signature not in seen:
                seen.add(signature)
                yield response, item, path

    def create(self, cls: type[Resource], item: dict[str, Any], id_field: str, name_field: str):
        resource = cls(
            identity=ResourceIdentity(
                account_id=self.account_id,
                region=AwsRegion.GLOBAL,
                service=AwsService.IAM,
                physical_id=item[id_field],
            ),
            name=item.get(name_field) if _text(item.get(name_field)) else None,
            arn=item["Arn"],
        )
        self.resources.append(resource)
        return resource

    def field(self, resource: Resource, pointer: str, response: IamResponse, path: str, value: Any):
        self.cite(resource, pointer, response.call.operation, path, value)

    def metadata(
        self, resource: Resource, origins: dict[str, tuple[IamResponse, str, Any]]
    ) -> None:
        fields = {
            "Arn": "/arn",
            "RoleId": "/identity/physical_id",
            "InstanceProfileId": "/identity/physical_id",
            "PolicyId": "/identity/physical_id",
            "RoleName": "/name",
            "InstanceProfileName": "/name",
            "PolicyName": "/name",
            "Path": "/attributes/path",
            "CreateDate": "/attributes/create_date",
            "MaxSessionDuration": "/attributes/max_session_duration",
        }
        for key, pointer in fields.items():
            if key not in origins:
                continue
            response, path, value = origins[key]
            if value is None:
                continue
            if pointer.startswith("/attributes/"):
                safe, _, _ = safe_tree(value)
                safe, _ = sanitize_value(json_safe(safe))
                resource.attributes[pointer.rsplit("/", 1)[-1]] = safe
            self.field(resource, pointer, response, path, value)
        if "Tags" in origins:
            response, path, value = origins["Tags"]
            safe, _, _ = safe_tree(value)
            safe, _ = redact_tag_pairs(json_safe(safe))
            safe, _ = sanitize_value(safe)
            if isinstance(safe, list):
                resource.attributes["tags"] = tags_of({"Tags": safe})
                self.field(resource, "/attributes/tags", response, path, value)

    def document(self, resource: Resource, response: IamResponse, path: str, value: Any):
        tree, _, invalid = safe_tree({"Document": value})
        for suffix in invalid:
            self.issue(
                response.call.operation,
                path + suffix.removeprefix(".Document"),
                _INVALID_DOCUMENT,
                resource,
            )
        return tree.get("Document")

    def roles(self) -> None:
        for response, item, path in self.selected(Op.LIST_ROLES, None, "RoleId", "RoleName"):
            role = self.create(IamRole, item, "RoleId", "RoleName")
            self.whole(role, response)
            origins = {key: (response, f"{path}.{key}", value) for key, value in item.items()}
            trust_source, trust_path = response, path + ".AssumeRolePolicyDocument"
            details = self.responses(Op.GET_ROLE, RoleName=role.name)
            if (
                len(details) != 1
                or not isinstance(details[0].value, dict)
                or not isinstance(details[0].value.get("Role"), dict)
            ):
                self.unavailable(role, Op.GET_ROLE, "Role details were unavailable or ambiguous.")
                for detail in details:
                    self.issue(
                        Op.GET_ROLE,
                        detail.path + ".Role",
                        "Role details were unavailable or ambiguous.",
                        role,
                    )
            else:
                detail = details[0]
                raw = detail.value["Role"]
                if any(
                    key in raw and raw[key] != item.get(key)
                    for key in ("RoleId", "Arn", "RoleName")
                ):
                    self.issue(
                        Op.GET_ROLE,
                        detail.path + ".Role",
                        "Conflicting role details were omitted.",
                        role,
                    )
                else:
                    self.whole(role, detail)
                    trust_source = detail
                    trust_path = detail.path + ".Role.AssumeRolePolicyDocument"
                    origins.update(
                        {
                            key: (detail, f"{detail.path}.Role.{key}", value)
                            for key, value in raw.items()
                        }
                    )
            self.metadata(role, origins)
            if "AssumeRolePolicyDocument" in origins:
                source, field_path, value = origins["AssumeRolePolicyDocument"]
                role.trust_document = self.document(role, source, field_path, value)
                if role.trust_document is not None:
                    self.field(role, "/trust_document", source, field_path, value)
            else:
                self.issue(trust_source.call.operation, trust_path, _MISSING_DOCUMENT, role)
            if "PermissionsBoundary" in origins:
                source, field_path, value = origins["PermissionsBoundary"]
                arn = value.get("PermissionsBoundaryArn") if isinstance(value, dict) else None
                if _text(arn):
                    role.boundary_arn = arn
                    self.field(
                        role, "/boundary_arn", source, field_path + ".PermissionsBoundaryArn", arn
                    )
            self.role_policies(role)

    def role_policies(self, role: IamRole) -> None:
        self.discovery_limit(role, Op.LIST_ATTACHED_ROLE_POLICIES)
        attachments = {}
        for response in self.responses(Op.LIST_ATTACHED_ROLE_POLICIES, RoleName=role.name):
            self.whole(role, response)
            arn = response.value.get("PolicyArn") if isinstance(response.value, dict) else None
            if _text(arn):
                attachments.setdefault(arn, response)
            else:
                self.issue(
                    Op.LIST_ATTACHED_ROLE_POLICIES,
                    response.path + ".PolicyArn",
                    "Invalid policy attachment was omitted.",
                    role,
                )
        role.attributes["attached_policy_arns"] = sorted(attachments)
        for index, arn in enumerate(sorted(attachments)):
            source = attachments[arn]
            self.field(
                role,
                f"/attributes/attached_policy_arns/{index}",
                source,
                source.path + ".PolicyArn",
                arn,
            )
        self.discovery_limit(role, Op.LIST_ROLE_POLICIES)
        names = {}
        for response in self.responses(Op.LIST_ROLE_POLICIES, RoleName=role.name):
            self.whole(role, response)
            if _text(response.value):
                names.setdefault(response.value, response)
            else:
                self.issue(
                    Op.LIST_ROLE_POLICIES,
                    response.path,
                    "Invalid inline policy name was omitted.",
                    role,
                )
        role.attributes["inline_policy_names"] = sorted(names)
        for index, name in enumerate(sorted(names)):
            source = names[name]
            self.field(role, f"/attributes/inline_policy_names/{index}", source, source.path, name)
            details = self.responses(Op.GET_ROLE_POLICY, RoleName=role.name, PolicyName=name)
            if len(details) != 1:
                self.unavailable(role, Op.GET_ROLE_POLICY, _MISSING_DOCUMENT)
                continue
            detail = details[0]
            self.whole(role, detail)
            raw = detail.value
            document_path = detail.path + ".PolicyDocument"
            if not isinstance(raw, dict) or "PolicyDocument" not in raw:
                self.issue(Op.GET_ROLE_POLICY, document_path, _MISSING_DOCUMENT, role)
                continue
            if any(
                key in raw and raw[key] != expected
                for key, expected in (("RoleName", role.name), ("PolicyName", name))
            ):
                self.issue(
                    Op.GET_ROLE_POLICY,
                    detail.path,
                    "Conflicting inline policy details were omitted.",
                    role,
                )
                continue
            document = self.document(role, detail, document_path, raw["PolicyDocument"])
            if document is not None:
                policy_index = len(role.inline_policies)
                role.inline_policies.append(IamInlinePolicy(name=name, document=document))
                self.field(
                    role,
                    f"/inline_policies/{policy_index}/document",
                    detail,
                    document_path,
                    raw["PolicyDocument"],
                )
                self.field(role, f"/inline_policies/{policy_index}/name", source, source.path, name)

    def profiles(self) -> None:
        for response, item, path in self.selected(
            Op.LIST_INSTANCE_PROFILES, None, "InstanceProfileId", "InstanceProfileName"
        ):
            profile = self.create(
                IamInstanceProfile, item, "InstanceProfileId", "InstanceProfileName"
            )
            self.whole(profile, response)
            self.metadata(
                profile, {key: (response, f"{path}.{key}", value) for key, value in item.items()}
            )
            roles = item.get("Roles", [])
            if not isinstance(roles, list):
                self.issue(
                    Op.LIST_INSTANCE_PROFILES,
                    path + ".Roles",
                    "Invalid profile roles were omitted.",
                    profile,
                )
                continue
            for source_index, role in enumerate(roles):
                arn = role.get("Arn") if isinstance(role, dict) else None
                role_path = f"{path}.Roles[{source_index}].Arn"
                if not _text(arn):
                    self.issue(
                        Op.LIST_INSTANCE_PROFILES,
                        role_path,
                        "Missing role ARN was omitted.",
                        profile,
                    )
                    continue
                index = len(profile.role_arns)
                profile.role_arns.append(arn)
                self.field(profile, f"/role_arns/{index}", response, role_path, arn)

    def policies(self) -> None:
        for response, item, path in self.selected(
            Op.GET_POLICY, "Policy", "PolicyId", "PolicyName"
        ):
            policy = self.create(IamManagedPolicy, item, "PolicyId", "PolicyName")
            self.whole(policy, response)
            self.metadata(
                policy, {key: (response, f"{path}.{key}", value) for key, value in item.items()}
            )
            version = item.get("DefaultVersionId")
            if not _text(version):
                self.issue(
                    Op.GET_POLICY,
                    path + ".DefaultVersionId",
                    "Default policy version was unavailable.",
                    policy,
                )
                self.unavailable(policy, Op.GET_POLICY_VERSION, _MISSING_DOCUMENT)
                continue
            policy.default_version_id = version
            self.field(policy, "/default_version_id", response, path + ".DefaultVersionId", version)
            versions = self.responses(
                Op.GET_POLICY_VERSION, PolicyArn=policy.arn, VersionId=version
            )
            if len(versions) != 1:
                self.unavailable(policy, Op.GET_POLICY_VERSION, _MISSING_DOCUMENT)
                continue
            detail = versions[0]
            self.whole(policy, detail)
            raw = detail.value.get("PolicyVersion") if isinstance(detail.value, dict) else None
            document_path = detail.path + ".PolicyVersion.Document"
            if not isinstance(raw, dict) or "Document" not in raw:
                self.issue(Op.GET_POLICY_VERSION, document_path, _MISSING_DOCUMENT, policy)
                continue
            if "VersionId" in raw and raw["VersionId"] != version:
                self.issue(
                    Op.GET_POLICY_VERSION,
                    detail.path + ".PolicyVersion.VersionId",
                    "Conflicting policy version was omitted.",
                    policy,
                )
                continue
            policy.document = self.document(policy, detail, document_path, raw["Document"])
            if policy.document is not None:
                self.field(policy, "/document", detail, document_path, raw["Document"])


def normalize_inventory(
    snapshot_id: UUID, account_id: str, inventory: IamInventory
) -> IamNormalized:
    normalizer = _Normalizer(snapshot_id, account_id, inventory)
    normalizer.roles()
    normalizer.profiles()
    normalizer.policies()
    return IamNormalized(
        resources=sorted(normalizer.resources, key=lambda resource: resource.id),
        evidence=dict(sorted(normalizer.evidence.items())),
        coverage=[normalizer.coverage[op] for op in sorted(normalizer.coverage)],
    )
