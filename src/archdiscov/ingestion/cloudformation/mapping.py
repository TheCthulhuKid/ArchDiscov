"""Allowlisted CloudFormation facts, without inventing deployed identifiers.

Inputs have already been intrinsic-evaluated and sanitized. Returned source
pointers are relative to Properties and cite retained scalar leaves only; the
caller cites Type and identity separately. Rules, policies, code, environment,
and nested-template payloads are intentionally not imported.
"""

from ipaddress import ip_network

from pydantic import ValidationError

from ...models.architecture import Subnet, UnknownResource, Vpc
from ...models.enums import AwsService
from ...models.resource_base import Resource, ResourceIdentity
from ..import_fields import TypedFields
from .catalog import RESOURCE_MAPPINGS, ResourceMapping


def service_for_type(aws_type: str) -> AwsService:
    """Resolve known service namespaces, including unsupported IAM resource types."""
    parts = aws_type.split("::")
    if len(parts) != 3 or parts[0] != "AWS":
        return AwsService.UNKNOWN
    return {
        "EC2": AwsService.EC2,
        "ElasticLoadBalancingV2": AwsService.ELASTIC_LOAD_BALANCING,
        "ElasticLoadBalancing": AwsService.ELASTIC_LOAD_BALANCING,
        "ECS": AwsService.ECS,
        "Lambda": AwsService.LAMBDA,
        "RDS": AwsService.RDS,
        "SQS": AwsService.SQS,
        "IAM": AwsService.IAM,
        "STS": AwsService.STS,
        "CloudFormation": AwsService.CLOUDFORMATION,
        "S3": AwsService.S3,
    }.get(parts[1], AwsService.UNKNOWN)


def _is_nonblank_unredacted_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value != "[REDACTED]"


class _Properties(TypedFields):
    def __init__(self, properties: dict, identity: ResourceIdentity):
        self.values = properties
        self.data: dict = {"identity": identity, "attributes": {}}
        self.facts: dict[str, str] = {}
        self.limitations: list[str] = []

    def source_pointer(self, source: str) -> str:
        return f"/{source}"

    def usable(self, value: object) -> bool:
        return _is_nonblank_unredacted_string(value)

    def omit_value(self, source: str) -> None:
        self.omit(source)

    def omit(self, source: str) -> None:
        self.limitations.append(f"Invalid or unresolved {source} omitted.")

    def string(self, source: str, target: str, *, attribute: bool = True) -> None:
        if source not in self.values:
            return
        value = self.values[source]
        if not _is_nonblank_unredacted_string(value):
            self.omit(source)
            return
        container = self.data["attributes"] if attribute else self.data
        # A target may name a nested field, such as a peering side's `requester/vpc_id`.
        *parents, leaf = target.split("/")
        for parent in parents:
            container = container.setdefault(parent, {})
        container[leaf] = value
        self.facts[f"/attributes/{target}" if attribute else f"/{target}"] = f"/{source}"

    def integer(self, source: str, target: str, minimum: int, maximum: int) -> None:
        if source not in self.values:
            return
        value = self.values[source]
        if type(value) is not int or not minimum <= value <= maximum:
            self.omit(source)
            return
        self.data["attributes"][target] = value
        self.facts[f"/attributes/{target}"] = f"/{source}"

    def name_tag(self) -> None:
        if "Tags" not in self.values:
            return
        tags = self.values["Tags"]
        if not isinstance(tags, list):
            self.omit("Tags")
            return
        for index, tag in enumerate(tags):
            if not isinstance(tag, dict):
                self.omit("Tags")
            elif tag.get("Key") == "Name":
                if _is_nonblank_unredacted_string(tag.get("Value")):
                    self.data["name"] = tag["Value"]
                    self.facts["/name"] = f"/Tags/{index}/Value"
                    return
                self.omit("Tags Name")

    def security_groups(self) -> None:
        # GroupSet is the NetworkInterface spelling; SecurityGroups contains
        # names for EC2 instances, so is deliberately not interpreted as IDs.
        for source in ("SecurityGroupIds", "GroupSet"):
            if source not in self.values:
                continue
            values = self.values[source]
            if not isinstance(values, list):
                self.omit(source)
                continue
            groups = self.data["attributes"].setdefault("security_group_ids", [])
            for index, value in enumerate(values):
                if not _is_nonblank_unredacted_string(value):
                    self.omit(source)
                    continue
                self.facts[f"/attributes/security_group_ids/{len(groups)}"] = f"/{source}/{index}"
                groups.append(value)
            if not groups:
                self.data["attributes"].pop("security_group_ids")

    def cidrs(self) -> None:
        blocks = []
        for source, version in (("CidrBlock", 4), ("Ipv6CidrBlock", 6)):
            if source not in self.values:
                continue
            value = self.values[source]
            try:
                if not _is_nonblank_unredacted_string(value):
                    raise ValueError
                network = ip_network(value)
                if network.version != version:
                    raise ValueError
            except ValueError:
                self.omit(source)
                continue
            self.facts[f"/cidr_blocks/{len(blocks)}"] = f"/{source}"
            blocks.append(network)
        if blocks:
            self.data["cidr_blocks"] = blocks


# A fact source meaning "the declared `Type`", for a value the type itself states.
TYPE_SOURCE = "#Type"


def _typed(model: type[Resource], mapping: ResourceMapping, fields: _Properties) -> Resource | None:
    """The typed resource, or None when its declared values break the model's rules."""
    claimed = {source for source, *_ in (*mapping.string_fields, *mapping.string_lists)}
    for source, target in (("VpcId", "vpc_id"), ("SubnetId", "subnet_id")):
        if source in claimed:
            continue
        # A model that declares the field records it there, as collection does.
        fields.string(source, target, attribute=target not in model.model_fields)
    if "SecurityGroupIds" not in claimed:
        fields.security_groups()
    if model in (Vpc, Subnet):
        fields.cidrs()
    for source, target in mapping.string_fields:
        fields.string(source, target, attribute=False)
    for source, target, spellings in mapping.choices:
        fields.choice(source, target, spellings)
    for source, target in mapping.string_lists:
        fields.string_list(source, target)
    for source, target in mapping.cidr_entries:
        fields.cidr_entries(source, target, "Cidr")
    for target, value in mapping.constants:
        fields.data[target] = value
        fields.facts[f"/{target}"] = TYPE_SOURCE
    if any(key in fields.values for key in ("SecurityGroupIngress", "SecurityGroupEgress")):
        fields.limitations.append("Inline security group rules are not mapped.")
    try:
        return model(**fields.data)
    except ValidationError:
        return None


def map_resource(
    aws_type: str, properties: dict, identity: ResourceIdentity
) -> tuple[Resource, dict[str, str], list[str]]:
    """Return a logical resource, safe leaf provenance, and payload-free limitations.

    Physical identifiers are never derived from logical addresses or configured
    names. The caller supplies identity and owns relationship/reference mapping.
    """
    fields = _Properties(properties if isinstance(properties, dict) else {}, identity)
    if not isinstance(properties, dict):
        fields.omit("Properties")
    mapping = RESOURCE_MAPPINGS.get(aws_type)
    model = mapping.model if mapping is not None else None
    fields.name_tag()
    if model is not None:
        resource = _typed(model, mapping, fields)
        if resource is not None:
            return resource, fields.facts, list(dict.fromkeys(fields.limitations))
        fields.limitations.append("Typed resource validation failed; only its type is retained.")
        fields.data = {"identity": identity, "attributes": {}}
        fields.facts = {}
        fields.name_tag()  # an unknown resource still keeps its safe Name tag
        resource = UnknownResource(aws_resource_type=aws_type, **fields.data)
    else:
        if mapping is not None:
            for source, target in mapping.string_fields:
                fields.string(source, target)
            for source, target, minimum, maximum in mapping.integer_fields:
                fields.integer(source, target, minimum, maximum)
        if aws_type == "AWS::CloudFormation::Stack":
            fields.limitations.append(
                "Nested stack contents are unavailable; "
                "TemplateURL and Parameters are not retained."
            )
        elif mapping is not None:
            fields.limitations.append(
                "No deployed ARN is available; only selected safe configuration facts are retained."
            )
        else:
            fields.limitations.append(
                "Unsupported CloudFormation resource type; "
                "only its type and safe Name tag are retained."
            )
        if service_for_type(aws_type) == AwsService.IAM:
            fields.limitations.append("IAM policy and trust policy documents are not mapped.")
        resource = UnknownResource(aws_resource_type=aws_type, **fields.data)
    return resource, fields.facts, list(dict.fromkeys(fields.limitations))
