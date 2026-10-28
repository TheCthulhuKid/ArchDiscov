"""Allowlisted Terraform resource mapping, without retaining provider payloads.

Fact sources are RFC 6901 pointers relative to the Terraform resource record:
``/type`` or ``/values/...``. The caller supplies identity/context citations.
Only leaf sources (or known empty/null containers) are cited, so evidence does
not accidentally copy discarded provider fields. Rules, routes, and IAM policy
documents are deliberately not imported by this initial mapping.
"""

from pydantic import ValidationError

from ...models.architecture import UnknownResource
from ...models.enums import AwsService
from ...models.resource_base import Resource, ResourceIdentity
from .catalog import RESOURCE_MAPPINGS
from .mapping_values import MappingValues


def map_resource(
    terraform_type: str, values: dict, identity: ResourceIdentity
) -> tuple[Resource, dict[str, str], list[str]]:
    """Map supported facts, returning safe omissions instead of raw validation errors."""
    facts = {"/resource_type": "/type", "/attributes/terraform_type": "/type"}
    limitations: list[str] = []
    mapping = RESOURCE_MAPPINGS.get(terraform_type)
    model = mapping.model if mapping else None

    def unknown() -> tuple[Resource, dict[str, str], list[str]]:
        facts.clear()
        facts.update(
            {
                "/resource_type": "/type",
                "/aws_resource_type": "/type",
                "/attributes/terraform_type": "/type",
            }
        )
        return (
            UnknownResource(
                identity=identity,
                aws_resource_type=mapping.aws_type if mapping else terraform_type,
                attributes={"terraform_type": terraform_type},
            ),
            facts,
            limitations,
        )

    if model is None:
        limitations.append(
            "Terraform resource type has no typed mapping; only its type is retained."
        )
        return unknown()

    data: dict = {"identity": identity, "attributes": {"terraform_type": terraform_type}}
    fields = MappingValues(values, data, facts, limitations)
    if mapping.service == AwsService.IAM and not fields.iam(mapping.string_fields):
        return unknown()
    fields.tags_and_name()
    claimed = {source for source, _ in mapping.string_fields}
    for field in ("vpc_id", "subnet_id", "iam_instance_profile"):
        if field in claimed:
            continue
        # A model that declares the field records it there, as collection does.
        fields.string(field, field, attribute=field not in model.model_fields)
    fields.security_groups()
    fields.cidrs(mapping.cidr_sources)
    if mapping.service != AwsService.IAM:
        for source, target in mapping.string_fields:
            fields.string(source, target)
    fields.addresses(mapping.address_lists)
    for source, target, spellings in mapping.choices:
        fields.choice(source, target, spellings)
    for source, target in mapping.string_lists:
        fields.string_list(source, target)
    for source, target, minimum in mapping.integer_fields:
        fields.integer(source, target, minimum)
    for target, value in mapping.constants:
        data[target] = value
        facts[f"/{target}"] = "/type"
    for source, target in mapping.cidr_entries:
        fields.cidr_entries(source, target, "cidr")
    fields.inline_collections(mapping.inline_collections)

    try:
        resource = model(**data)
    except ValidationError:
        limitations.append("Typed resource validation failed; only its type is retained.")
        return unknown()
    return resource, facts, list(dict.fromkeys(limitations))
