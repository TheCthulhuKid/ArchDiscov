"""Normalize nested ELBv2 values without discarding their source positions."""

from typing import Any

from ...models.enums import ListenerActionType, TargetHealthState
from ...models.resources.load_balancing_values import (
    ListenerAction,
    RegisteredTarget,
    RuleCondition,
    WeightedTargetGroup,
)
from .sanitization import sanitize_source


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def port(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 65535


def enum_value(enum: type, value: Any) -> bool:
    return isinstance(value, str) and value in enum._value2member_map_


def actions(n, resource, op, item, path, key):
    entries = n.read(resource, op, item, path, key, lambda value: isinstance(value, list))
    if entries is None:
        return None
    result = []
    for source_index, entry in enumerate(entries):
        source = f"{path}.{key}[{source_index}]"
        if not isinstance(entry, dict):
            n.issue(op, source, "Malformed action was omitted", resource)
            continue
        kind = n.read(
            resource, op, entry, source, "Type", lambda v: enum_value(ListenerActionType, v)
        )
        if kind is None:
            continue
        pointer = f"/actions/{len(result)}"
        order = n.field(
            resource,
            op,
            entry,
            source,
            "Order",
            f"{pointer}/order",
            lambda v: type(v) is int,
            required=False,
        )
        direct = n.field(
            resource,
            op,
            entry,
            source,
            "TargetGroupArn",
            f"{pointer}/target_group_arn",
            text,
            required=False,
        )
        groups = []
        if "ForwardConfig" in entry:
            config = n.read(
                resource, op, entry, source, "ForwardConfig", lambda v: isinstance(v, dict)
            )
            if config is not None:
                group_entries = n.read(
                    resource,
                    op,
                    config,
                    f"{source}.ForwardConfig",
                    "TargetGroups",
                    lambda v: isinstance(v, list),
                )
                for group_index, group in enumerate(group_entries or []):
                    group_path = f"{source}.ForwardConfig.TargetGroups[{group_index}]"
                    if not isinstance(group, dict):
                        n.issue(
                            op, group_path, "Malformed weighted target group was omitted", resource
                        )
                        continue
                    group_pointer = f"{pointer}/forward_groups/{len(groups)}"
                    arn = n.field(
                        resource,
                        op,
                        group,
                        group_path,
                        "TargetGroupArn",
                        f"{group_pointer}/arn",
                        text,
                    )
                    if arn is None:
                        continue
                    weight = n.field(
                        resource,
                        op,
                        group,
                        group_path,
                        "Weight",
                        f"{group_pointer}/weight",
                        lambda v: type(v) is int and 0 <= v <= 999,
                        required=False,
                    )
                    groups.append(WeightedTargetGroup(arn=arn, weight=weight))
        elif kind == "forward" and direct is None:
            n.issue(op, f"{source}.TargetGroupArn", "Forward destination is unavailable", resource)
        wrapper = {
            "authenticate-oidc": "AuthenticateOidcConfig",
            "authenticate-cognito": "AuthenticateCognitoConfig",
            "redirect": "RedirectConfig",
            "fixed-response": "FixedResponseConfig",
        }.get(kind)
        if wrapper is not None:
            n.read(resource, op, entry, source, wrapper, lambda v: isinstance(v, dict))
        safe, _ = sanitize_source(entry)
        configuration = {
            k: v for k, v in safe.items() if k not in ("Type", "Order", "TargetGroupArn")
        }
        result.append(
            ListenerAction(
                type=kind,
                order=order,
                target_group_arn=direct,
                forward_groups=groups,
                configuration=configuration,
            )
        )
        n.cite(resource, pointer, op, source, entry)
        n.cite(resource, f"{pointer}/type", op, f"{source}.Type", kind)
    return result


_CONDITION_CONFIGS = {
    "host-header": "HostHeaderConfig",
    "path-pattern": "PathPatternConfig",
    "http-header": "HttpHeaderConfig",
    "http-request-method": "HttpRequestMethodConfig",
    "query-string": "QueryStringConfig",
    "source-ip": "SourceIpConfig",
}


def conditions(n, resource, op, item, path):
    entries = n.read(resource, op, item, path, "Conditions", lambda v: isinstance(v, list))
    if entries is None:
        return None
    result = []
    for source_index, entry in enumerate(entries):
        source = f"{path}.Conditions[{source_index}]"
        if not isinstance(entry, dict):
            n.issue(op, source, "Malformed rule condition was omitted", resource)
            continue
        field = n.read(
            resource, op, entry, source, "Field", lambda v: text(v) and v in _CONDITION_CONFIGS
        )
        if field is None:
            continue
        wrapper = _CONDITION_CONFIGS[field]
        if wrapper in entry:
            n.read(resource, op, entry, source, wrapper, lambda v: isinstance(v, dict))
        elif "Values" in entry:
            n.read(resource, op, entry, source, "Values", lambda v: isinstance(v, list))
        elif "RegexValues" in entry:
            n.read(resource, op, entry, source, "RegexValues", lambda v: isinstance(v, list))
        else:
            n.issue(op, f"{source}.{wrapper}", "Condition configuration is unavailable", resource)
        safe, _ = sanitize_source(entry)
        pointer = f"/conditions/{len(result)}"
        result.append(
            RuleCondition(
                field=field, configuration={k: v for k, v in safe.items() if k != "Field"}
            )
        )
        n.cite(resource, pointer, op, source, entry)
        n.cite(resource, f"{pointer}/field", op, f"{source}.Field", field)
    return result


def target(n, resource, response, index):
    op = response.call.operation
    source = response.path
    entry = response.value
    if not isinstance(entry, dict):
        n.issue(op, source, "Malformed target registration was omitted", resource)
        return None
    target_item = n.read(resource, op, entry, source, "Target", lambda v: isinstance(v, dict))
    if target_item is None:
        return None
    pointer = f"/targets/{index}"
    target_path = f"{source}.Target"
    identifier = n.field(resource, op, target_item, target_path, "Id", f"{pointer}/id", text)
    if identifier is None:
        return None
    fields = {
        "id": identifier,
        "port": n.field(
            resource, op, target_item, target_path, "Port", f"{pointer}/port", port, required=False
        ),
        "availability_zone": n.field(
            resource,
            op,
            target_item,
            target_path,
            "AvailabilityZone",
            f"{pointer}/availability_zone",
            text,
            required=False,
        ),
    }
    health = n.read(resource, op, entry, source, "TargetHealth", lambda v: isinstance(v, dict))
    if health is not None:
        for source_key, field, validator, required in (
            ("State", "health_state", lambda v: enum_value(TargetHealthState, v), True),
            ("Reason", "health_reason", text, False),
            ("Description", "health_description", text, False),
        ):
            fields[field] = n.field(
                resource,
                op,
                health,
                f"{source}.TargetHealth",
                source_key,
                f"{pointer}/{field}",
                validator,
                required=required,
            )
    n.cite(resource, pointer, op, source, entry)
    return RegisteredTarget(**fields)
