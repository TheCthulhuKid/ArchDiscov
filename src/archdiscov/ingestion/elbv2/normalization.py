"""Evidence-backed, flat ELBv2 inventory normalization."""

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ...models.architecture import (
    Coverage,
    Evidence,
    Resource,
    ResourceIdentity,
    UnknownResource,
    UnresolvedValue,
)
from ...models.enums import (
    AwsRegion,
    AwsService,
    CoverageStatus,
    LoadBalancerProtocol,
    LoadBalancerScheme,
    LoadBalancerType,
    ResourceType,
    SourceType,
    TargetType,
)
from ...models.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup
from ..evidence_builder import EvidenceBuilder
from ..operations import Elbv2Operation as Op
from .collector import Elbv2Inventory
from .sanitization import sanitize_source
from .values import actions, conditions, enum_value, port, target, text


@dataclass
class Elbv2Normalized:
    resources: list[Resource]
    evidence: dict[str, Evidence]
    coverage: list[Coverage]


def _request_path(params: dict[str, Any]) -> str:
    return f"Request[{json.dumps(params, sort_keys=True, separators=(',', ':'))}]"


class _Normalizer(EvidenceBuilder):
    def __init__(self, snapshot_id, account_id, region, inventory):
        super().__init__(snapshot_id, account_id, region, AwsService.ELASTIC_LOAD_BALANCING)
        self.inventory = inventory
        self.resources: dict[str, Resource] = {}
        self.coverage = {
            op: result.coverage.model_copy(deep=True) for op, result in inventory.results.items()
        }

    def _evidence(self, key: str, resource_id: str, operation: str, path: str, value: Any) -> str:
        safe, redacted_paths = sanitize_source(value)
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

    def issue(self, op, path, reason, resource=None):
        if resource is not None:
            limitations = resource.attributes.setdefault("limitations", [])
            limitation = f"{op}: {path}: {reason}"
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
        if unresolved in coverage.unresolved_values:
            return
        fields = coverage.model_dump()
        fields["unresolved_values"] = [*coverage.unresolved_values, unresolved]
        if coverage.status == CoverageStatus.SUCCESS:
            fields["status"] = CoverageStatus.INCOMPLETE
        self.coverage[op] = Coverage(**fields)

    def read(self, resource, op, item, path, key, validator, *, required=True):
        if key not in item and not required:
            return None
        value = item.get(key)
        if not validator(value):
            self.issue(op, f"{path}.{key}", "Missing, malformed, or unsupported value", resource)
            return None
        return value

    def field(self, resource, op, item, path, key, pointer, validator, *, required=True):
        value = self.read(resource, op, item, path, key, validator, required=required)
        if value is not None:
            self.cite(resource, pointer, op, f"{path}.{key}", value)
        return value

    def whole(self, resource, response):
        evidence_id = self.source_evidence(
            resource, response.call.operation, response.path, response.value
        )
        if evidence_id not in resource.evidence_ids:
            resource.evidence_ids.append(evidence_id)

    def entries(self, op, id_field):
        result = self.inventory.results.get(op)
        if result is None:
            return
        candidates = {}
        kind = {
            "LoadBalancerArn": r"loadbalancer/(?:app|net|gwy)/[A-Za-z0-9-]+/[A-Za-z0-9]+",
            "ListenerArn": r"listener/(?:app|net)/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
            "RuleArn": r"listener-rule/app/[A-Za-z0-9-]+/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
            "TargetGroupArn": r"targetgroup/[A-Za-z0-9-]+/[A-Za-z0-9]+",
        }[id_field]
        prefix = (
            rf"arn:aws[a-z-]*:elasticloadbalancing:{re.escape(self.region.value)}:"
            rf"{self.account_id}:"
        )
        for response in result.responses:
            item = response.value
            arn = item.get(id_field) if isinstance(item, dict) else None
            if not text(arn) or not re.fullmatch(prefix + kind, arn):
                self.issue(op, f"{response.path}.{id_field}", "Invalid resource identity; omitted")
                continue
            candidates.setdefault(arn, []).append(response)
        for arn, entries in candidates.items():
            if any(
                entry.value != entries[0].value or entry.call.params != entries[0].call.params
                for entry in entries[1:]
            ):
                for entry in entries:
                    self.issue(
                        op, f"{entry.path}.{id_field}", "Conflicting resource identity; omitted"
                    )
                continue
            yield arn, entries

    def create(self, cls, physical_id, response, **fields):
        safe, _ = sanitize_source(response.value)
        resource = cls(
            identity=ResourceIdentity(
                account_id=self.account_id,
                region=self.region,
                service=self.service,
                physical_id=physical_id,
            ),
            attributes={"summary": safe},
            **fields,
        )
        self.resources[physical_id] = resource
        return resource

    def scalar_fields(self, resource, response, fields):
        for key, field, validator, required in fields:
            value = self.field(
                resource,
                response.call.operation,
                response.value,
                response.path,
                key,
                f"/{field}",
                validator,
                required=required,
            )
            enum = {
                "lb_type": LoadBalancerType,
                "scheme": LoadBalancerScheme,
                "protocol": LoadBalancerProtocol,
                "target_type": TargetType,
            }.get(field)
            if value is not None and enum is not None:
                value = enum(value)
            setattr(resource, field, value)

    def parent(self, resource, response, key, field):
        item = response.value
        op = response.call.operation
        if key in item:
            value = self.field(resource, op, item, response.path, key, f"/{field}", text)
        else:
            request = _request_path(response.call.params)
            value = self.field(resource, op, response.call.params, request, key, f"/{field}", text)
        setattr(resource, field, value)

    def load_balancers(self):
        op = Op.DESCRIBE_LOAD_BALANCERS
        for arn, entries in self.entries(op, "LoadBalancerArn"):
            supported = entries[0].value.get("Type") in ("application", "network")
            if supported:
                resource = self.create(LoadBalancer, arn, entries[0], arn=arn)
            else:
                resource = self.create(
                    UnknownResource,
                    arn,
                    entries[0],
                    aws_resource_type=ResourceType.ELBV2_LOAD_BALANCER,
                )
            for response in entries:
                item, path = response.value, response.path
                self.whole(resource, response)
                self.cite(
                    resource,
                    "/arn" if supported else "/identity/physical_id",
                    op,
                    f"{path}.LoadBalancerArn",
                    arn,
                )
                if not supported:
                    self.issue(
                        op, f"{path}.Type", "Unsupported load balancer; summary retained", resource
                    )
                    continue
                self.scalar_fields(
                    resource,
                    response,
                    (
                        ("LoadBalancerName", "name", text, False),
                        ("Type", "lb_type", lambda v: v in ("application", "network"), True),
                        ("Scheme", "scheme", lambda v: enum_value(LoadBalancerScheme, v), True),
                        ("DNSName", "dns_name", text, True),
                        (
                            "IpAddressType",
                            "ip_address_type",
                            lambda v: v in ("ipv4", "dualstack", "dualstack-without-public-ipv4"),
                            True,
                        ),
                        ("VpcId", "vpc_id", text, True),
                    ),
                )
                zones = self.read(
                    resource, op, item, path, "AvailabilityZones", lambda v: isinstance(v, list)
                )
                if zones is not None:
                    resource.attributes["availability_zones"] = sanitize_source(zones)[0]
                    self.cite(
                        resource,
                        "/attributes/availability_zones",
                        op,
                        f"{path}.AvailabilityZones",
                        zones,
                    )
                    resource.subnet_ids = []
                    for index, zone in enumerate(zones):
                        zone_path = f"{path}.AvailabilityZones[{index}]"
                        if not isinstance(zone, dict):
                            self.issue(op, zone_path, "Malformed availability zone", resource)
                            continue
                        subnet = self.field(
                            resource,
                            op,
                            zone,
                            zone_path,
                            "SubnetId",
                            f"/subnet_ids/{len(resource.subnet_ids)}",
                            text,
                        )
                        if subnet is not None:
                            resource.subnet_ids.append(subnet)
                security_groups = self.read(
                    resource,
                    op,
                    item,
                    path,
                    "SecurityGroups",
                    lambda v: isinstance(v, list),
                    required=resource.lb_type == "application",
                )
                if security_groups is not None:
                    resource.security_group_ids = []
                    for index, group in enumerate(security_groups):
                        source = f"{path}.SecurityGroups[{index}]"
                        if not text(group):
                            self.issue(op, source, "Malformed security group ID", resource)
                            continue
                        self.cite(
                            resource,
                            f"/security_group_ids/{len(resource.security_group_ids)}",
                            op,
                            source,
                            group,
                        )
                        resource.security_group_ids.append(group)
                state = self.read(resource, op, item, path, "State", lambda v: isinstance(v, dict))
                if state is not None:
                    resource.attributes["state"] = sanitize_source(state)[0]
                    self.cite(resource, "/attributes/state", op, f"{path}.State", state)

    def listeners(self):
        op = Op.DESCRIBE_LISTENERS
        for arn, entries in self.entries(op, "ListenerArn"):
            resource = self.create(Listener, arn, entries[0], arn=arn)
            for response in entries:
                self.whole(resource, response)
                self.cite(resource, "/arn", op, f"{response.path}.ListenerArn", arn)
                self.parent(resource, response, "LoadBalancerArn", "load_balancer_arn")
                self.scalar_fields(
                    resource,
                    response,
                    (
                        ("Port", "port", port, True),
                        (
                            "Protocol",
                            "protocol",
                            lambda v: enum_value(LoadBalancerProtocol, v),
                            True,
                        ),
                    ),
                )
                resource.actions = actions(
                    self, resource, op, response.value, response.path, "DefaultActions"
                )

    def rules(self):
        op = Op.DESCRIBE_RULES
        for arn, entries in self.entries(op, "RuleArn"):
            resource = self.create(ListenerRule, arn, entries[0], arn=arn)
            for response in entries:
                self.whole(resource, response)
                self.cite(resource, "/arn", op, f"{response.path}.RuleArn", arn)
                self.parent(resource, response, "ListenerArn", "listener_arn")
                self.scalar_fields(
                    resource,
                    response,
                    (
                        ("Priority", "priority", text, True),
                        ("IsDefault", "is_default", lambda v: type(v) is bool, True),
                    ),
                )
                resource.conditions = conditions(self, resource, op, response.value, response.path)
                resource.actions = actions(
                    self, resource, op, response.value, response.path, "Actions"
                )

    def target_groups(self):
        op = Op.DESCRIBE_TARGET_GROUPS
        for arn, entries in self.entries(op, "TargetGroupArn"):
            resource = self.create(TargetGroup, arn, entries[0], arn=arn)
            for response in entries:
                self.whole(resource, response)
                self.cite(resource, "/arn", op, f"{response.path}.TargetGroupArn", arn)
                is_lambda = response.value.get("TargetType") == "lambda"
                self.scalar_fields(
                    resource,
                    response,
                    (
                        ("TargetGroupName", "name", text, False),
                        ("TargetType", "target_type", lambda v: enum_value(TargetType, v), True),
                        ("VpcId", "vpc_id", text, not is_lambda),
                        (
                            "Protocol",
                            "protocol",
                            lambda v: enum_value(LoadBalancerProtocol, v),
                            not is_lambda,
                        ),
                        ("Port", "port", port, not is_lambda),
                    ),
                )
                health = {}
                for key in (
                    "HealthCheckEnabled",
                    "HealthCheckProtocol",
                    "HealthCheckPort",
                    "HealthCheckPath",
                    "HealthCheckIntervalSeconds",
                    "HealthCheckTimeoutSeconds",
                    "HealthyThresholdCount",
                    "UnhealthyThresholdCount",
                    "Matcher",
                ):
                    if key not in response.value:
                        continue
                    validator = self.health_validator(key)
                    observed = self.field(
                        resource,
                        op,
                        response.value,
                        response.path,
                        key,
                        f"/health_check/{key}",
                        validator,
                    )
                    if observed is not None:
                        health[key] = sanitize_source({key: observed})[0][key]
                if not is_lambda and response.value.get("HealthCheckEnabled") is not False:
                    for key in ("HealthCheckProtocol", "HealthCheckPort"):
                        if key not in response.value:
                            self.issue(
                                op,
                                f"{response.path}.{key}",
                                "Health check field is unavailable",
                                resource,
                            )
                if not health:
                    self.issue(
                        op,
                        f"{response.path}.HealthCheckProtocol",
                        "Health check configuration is unavailable",
                        resource,
                    )
                resource.health_check = health or None
            self.target_health(resource)

    @staticmethod
    def health_validator(key):
        if key == "Matcher":
            return lambda v: isinstance(v, dict)
        if key == "HealthCheckEnabled":
            return lambda v: type(v) is bool
        if key in (
            "HealthCheckIntervalSeconds",
            "HealthCheckTimeoutSeconds",
            "HealthyThresholdCount",
            "UnhealthyThresholdCount",
        ):
            return lambda v: type(v) is int and v > 0
        if key == "HealthCheckProtocol":
            return lambda v: enum_value(LoadBalancerProtocol, v)
        if key == "HealthCheckPort":
            return lambda v: (
                text(v)
                and (
                    v == "traffic-port" or (v.isascii() and v.isdecimal() and 1 <= int(v) <= 65535)
                )
            )
        return text

    def target_health(self, resource):
        op = Op.DESCRIBE_TARGET_HEALTH
        result = self.inventory.results.get(op)
        entries = (
            []
            if result is None
            else [
                response
                for response in result.responses
                if response.call.params.get("TargetGroupArn") == resource.arn
            ]
        )
        request = _request_path({"TargetGroupArn": resource.arn})
        path = f"{request}.Page[0].TargetHealthDescriptions"
        if entries:
            resource.attributes["target_health"] = [sanitize_source(r.value)[0] for r in entries]
            resource.targets = []
            for response in entries:
                self.whole(resource, response)
                registration = target(self, resource, response, len(resource.targets))
                if registration is not None:
                    resource.targets.append(registration)
            if not resource.targets:
                resource.targets = None
        elif (
            result is not None
            and result.coverage.status == CoverageStatus.SUCCESS
            and resource.target_type is not None
        ):
            resource.targets = []
            self.cite(resource, "/targets", op, path, [])
        else:
            self.issue(
                op,
                path,
                "Target health is unavailable; empty per-group success cannot be "
                "distinguished from an unreturned or failed call",
                resource,
            )
        if result is not None and result.coverage.status != CoverageStatus.SUCCESS:
            self.issue(op, path, "Target health collection was unavailable or incomplete", resource)

    def tags(self):
        op = Op.DESCRIBE_TAGS
        result = self.inventory.results.get(op)
        returned = set()
        if result is not None:
            for response in result.responses:
                item = response.value
                arn = item.get("ResourceArn") if isinstance(item, dict) else None
                resource = self.resources.get(arn) if text(arn) else None
                if resource is None or arn not in response.call.params.get("ResourceArns", []):
                    self.issue(
                        op, f"{response.path}.ResourceArn", "Unexpected tag identity; omitted"
                    )
                    continue
                tags = self.read(
                    resource, op, item, response.path, "Tags", lambda v: isinstance(v, list)
                )
                if tags is None:
                    continue
                safe, _ = sanitize_source(item)
                normalized = {}
                valid = True
                for index, tag in enumerate(tags):
                    if (
                        not isinstance(tag, dict)
                        or not isinstance(tag.get("Key"), str)
                        or not isinstance(tag.get("Value"), str)
                    ):
                        self.issue(op, f"{response.path}.Tags[{index}]", "Malformed tag", resource)
                        valid = False
                        continue
                    normalized[tag["Key"]] = safe["Tags"][index]["Value"]
                if valid:
                    returned.add(arn)
                    resource.attributes["tags"] = normalized
                    self.cite(resource, "/attributes/tags", op, f"{response.path}.Tags", tags)
        for arn, resource in self.resources.items():
            if arn not in returned:
                existing = (
                    []
                    if result is None
                    else [
                        value.configuration_path
                        for value in result.coverage.unresolved_values
                        if f"ResourceArn={arn}" in value.configuration_path
                    ]
                )
                path = existing[0] if existing else (f"TagDescriptions[ResourceArn={arn}]")
                self.issue(
                    op, path, "Resource tags are unavailable; no empty tags substituted", resource
                )


def normalize_inventory(
    snapshot_id: UUID, account_id: str, region: AwsRegion, inventory: Elbv2Inventory
) -> Elbv2Normalized:
    normalizer = _Normalizer(snapshot_id, account_id, AwsRegion(region), inventory)
    normalizer.load_balancers()
    normalizer.listeners()
    normalizer.rules()
    normalizer.target_groups()
    normalizer.tags()
    return Elbv2Normalized(
        resources=list(normalizer.resources.values()),
        evidence=normalizer.evidence,
        coverage=list(normalizer.coverage.values()),
    )
