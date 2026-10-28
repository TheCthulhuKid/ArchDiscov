"""Map evaluated declarations and record only supported, sanitized source facts."""

from ...models.enums import AwsRegion, AwsService, ResourceType
from ...models.json_pointer import escape_pointer_token, read_pointer
from ...models.resource_base import ResourceIdentity
from .context import safe_properties
from .evaluation import evaluate
from .issue_codes import EvaluationIssueCode
from .mapping import TYPE_SOURCE, map_resource, service_for_type
from .relationships import link


def _record_facts(builder, resource, logical_id, entry, values, pointers, evaluated):
    path = f"/Resources/{escape_pointer_token(logical_id)}"
    resource.fact_evidence["/identity/logical_address"] = builder.cite(
        resource, path, {"logical_id": logical_id}
    )
    pointer = (
        "/aws_resource_type" if resource.resource_type == ResourceType.UNKNOWN else "/resource_type"
    )
    resource.fact_evidence[pointer] = builder.cite(resource, path + "/Type", entry["Type"])
    for pointer, source in sorted(pointers.items()):
        if source == TYPE_SOURCE:
            resource.fact_evidence[pointer] = builder.cite(resource, path + "/Type", entry["Type"])
            continue
        resource.fact_evidence[pointer] = builder.cite(
            resource,
            path + "/Properties" + evaluated.sources.get(source, source),
            read_pointer(values, source),
        )


def _record_limitations(builder, resource, path, values, pointers, evaluated, redactions, reasons):
    retained = {source.split("/")[1] for source in pointers.values() if source != TYPE_SOURCE}
    for field in values:
        if escape_pointer_token(field) not in retained:
            builder.limit(
                resource,
                path + "/Properties/" + escape_pointer_token(field),
                "Property is outside the supported mapping",
            )
    for redacted in sorted(set(redactions + evaluated.redacted_paths)):
        source_path = path + "/Properties" + redacted
        builder.cite(resource, source_path, "[REDACTED]", redacted=True)
        builder.limit(resource, source_path, "Sensitive source value redacted")
    for issue in evaluated.issues:
        issue_path = path + "/Properties" + issue.path
        builder.cite(resource, issue_path, reason=issue.reason)
        builder.limit(resource, issue_path, issue.reason)
        if issue.reason in (
            EvaluationIssueCode.IMPORT_UNAVAILABLE,
            EvaluationIssueCode.REFERENCE_UNAVAILABLE,
        ):
            link(builder, resource, issue_path, reason=issue.reason)
    for reason in reasons:
        builder.limit(resource, path + "/Properties", reason)


def normalize_resource(builder, logical_id, entry, options):
    request = builder.request
    path = f"/Resources/{escape_pointer_token(logical_id)}"
    props, redactions = safe_properties(entry.get("Properties", {}))
    evaluated = evaluate(props, **options)
    values, evaluated_redactions = safe_properties(
        evaluated.value if isinstance(evaluated.value, dict) else {}
    )
    service = service_for_type(entry["Type"])
    prefix = request.stack_id or request.stack_name
    identity = ResourceIdentity(
        account_id=request.account_id,
        region=AwsRegion.GLOBAL if service == AwsService.IAM else request.region,
        service=service,
        logical_address=f"{prefix}/{logical_id}" if prefix else logical_id,
    )
    resource, pointers, reasons = map_resource(entry["Type"], values, identity)
    builder.resources[logical_id] = resource
    _record_facts(builder, resource, logical_id, entry, values, pointers, evaluated)
    _record_limitations(
        builder,
        resource,
        path,
        values,
        pointers,
        evaluated,
        redactions + [evaluated.sources.get(p, p) for p in evaluated_redactions],
        reasons,
    )
    if "Metadata" in entry or "CreationPolicy" in entry or "UpdatePolicy" in entry:
        builder.limit(resource, path, "Resource metadata and execution policies are not evaluated")
    return evaluated.references
