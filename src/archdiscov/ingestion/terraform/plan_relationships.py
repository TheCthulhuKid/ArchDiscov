"""Plan-side containment and references, without evaluating HCL expressions."""

import re
from collections import defaultdict

from ...models.architecture import Dependency, LocalTarget, RegionArchitecture, UnresolvedTarget
from ...models.enums import AttachmentKind, DependencyRelationship, LifecycleValueScope
from ...models.json_pointer import escape_pointer_token
from ..adapter import IngestionError
from .relationships import place_resources

_INDEX = re.compile(r'\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\]')


def place_plan_resources(resources, evidence):
    regions, globals_, dependencies = {}, [], defaultdict(list)
    for scope in LifecycleValueScope:
        selected = [resource for resource in resources if resource.lifecycle.value_scope == scope]
        placed, global_resources, links = place_resources(selected, evidence)
        for region, tree in placed.items():
            regions.setdefault(region, RegionArchitecture()).resources.extend(tree.resources)
        globals_.extend(global_resources)
        for region, group in links.items():
            dependencies[region].extend(group)
    return regions, globals_, dict(dependencies)


def _references(value, path, depth=0):
    if depth > 64:
        raise IngestionError(
            "invalid_request", "Terraform expression nesting exceeds supported limits"
        )
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{path}/{escape_pointer_token(key)}"
            if key == "references":
                if not isinstance(child, list) or any(
                    not isinstance(item, str) or not item.strip() for item in child
                ):
                    raise IngestionError(
                        "invalid_request", "Terraform expression references must be strings"
                    )
                for index, reference in enumerate(child):
                    yield reference, f"{location}/{index}"
            elif key != "constant_value":
                yield from _references(child, location, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _references(child, f"{path}/{index}", depth + 1)


def _configuration_resources(module, path, prefix="", depth=0):
    if depth > 64 or not isinstance(module, dict):
        raise IngestionError("invalid_request", "Invalid Terraform configuration module")
    resources = module.get("resources", [])
    calls = module.get("module_calls", {})
    if not isinstance(resources, list) or not isinstance(calls, dict):
        raise IngestionError("invalid_request", "Invalid Terraform configuration collections")
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict) or not isinstance(resource.get("address"), str):
            raise IngestionError("invalid_request", "Invalid Terraform configuration resource")
        if resource.get("mode", "managed") != "managed":
            continue
        location = f"{path}/resources/{index}/expressions"
        expressions = resource.get("expressions", {})
        if not isinstance(expressions, dict):
            raise IngestionError(
                "invalid_request", "Terraform configuration expressions must be objects"
            )
        address = resource["address"]
        yield (
            address if address.startswith(prefix) else prefix + address,
            location,
            expressions,
            prefix,
        )
    for name, call in calls.items():
        if not isinstance(call, dict):
            raise IngestionError("invalid_request", "Invalid Terraform module call")
        if "module" in call:
            yield from _configuration_resources(
                call["module"],
                f"{path}/module_calls/{escape_pointer_token(name)}/module",
                f"{prefix}module.{name}.",
                depth + 1,
            )


def configuration_dependencies(data, builder):
    configuration = data.get("configuration", {})
    if not isinstance(configuration, dict):
        raise IngestionError("invalid_request", "Terraform configuration must be an object")
    modules = _configuration_resources(
        configuration.get("root_module", {}), "/configuration/root_module"
    )
    by_address = defaultdict(list)
    by_configuration_address = defaultdict(list)
    for resource in builder.resources:
        if resource.lifecycle.value_scope != LifecycleValueScope.AFTER:
            continue
        address = resource.identity.logical_address
        by_address[address].append(resource)
        by_configuration_address[_INDEX.sub("", address)].append(resource)
    links = defaultdict(list)
    for address, path, expressions, prefix in modules:
        sources = by_configuration_address.get(address, ())
        for reference, location in _references(expressions, path):
            # A traversal can mention a resource and then its attribute; indexed targets
            # must match exactly. No evaluation of variables, counts, or module outputs.
            qualified = prefix + reference
            targets = list(by_address.get(qualified, ()))
            for index, character in enumerate(qualified):
                if character == ".":
                    targets.extend(by_address.get(qualified[:index], ()))
            for source in sources:
                target = targets[0] if len(targets) == 1 and targets[0] is not source else None
                citations = builder.cite(
                    source, location, reference if target else None, unresolved=target is None
                )
                links[source.identity.region].append(
                    Dependency(
                        source_id=source.id,
                        target=LocalTarget(resource_id=target.id)
                        if target
                        else UnresolvedTarget(
                            reason=(
                                "Configuration reference is unavailable, ambiguous, "
                                "or requires expression evaluation"
                            ),
                            source_path=location,
                        ),
                        relationship=DependencyRelationship.ATTACHED_TO,
                        attributes={
                            "kind": AttachmentKind.TERRAFORM_REFERENCE,
                            "field": "configuration_expression",
                        },
                        evidence_ids=citations,
                    )
                )
    return links
