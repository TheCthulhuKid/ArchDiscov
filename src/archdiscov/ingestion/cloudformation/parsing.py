import json
from pathlib import Path
from typing import Any

import yaml
from yaml.events import CollectionEndEvent, CollectionStartEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from ..adapter import IngestionError
from ..strict_json import reject_constant, unique_object
from .evaluation_limits import MAX_BYTES, MAX_DEPTH, MAX_NODES, validate_json

_MESSAGE = "Invalid CloudFormation template."
_INTRINSICS = (
    "Ref",
    "GetAtt",
    "Sub",
    "Join",
    "Select",
    "Split",
    "FindInMap",
    "If",
    "Equals",
    "And",
    "Or",
    "Not",
    "ImportValue",
    "GetAZs",
    "Base64",
    "Condition",
)


class _TemplateLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        if not isinstance(node, MappingNode):
            raise ValueError
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str or key in result:
                raise ValueError
            if (
                key == "AWSTemplateFormatVersion"
                and value_node.tag == "tag:yaml.org,2002:timestamp"
            ):
                result[key] = self.construct_scalar(value_node)
            else:
                result[key] = self.construct_object(value_node, deep=deep)
        return result


def _intrinsic(loader: _TemplateLoader, node: Any) -> dict:
    name = node.tag[1:]
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node)
    else:
        raise ValueError
    if name == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {name if name in ("Ref", "Condition") else f"Fn::{name}": value}


for _name in _INTRINSICS:
    _TemplateLoader.add_constructor(f"!{_name}", _intrinsic)


def _validate_template(template: Any) -> None:
    validate_json(template)
    if not isinstance(template, dict) or not isinstance(template.get("Resources"), dict):
        raise ValueError
    if (
        "AWSTemplateFormatVersion" in template
        and template["AWSTemplateFormatVersion"] != "2010-09-09"
    ):
        raise ValueError
    for name, resource in template["Resources"].items():
        if not name or not isinstance(resource, dict):
            raise ValueError
        if not isinstance(resource.get("Type"), str) or not resource["Type"].strip():
            raise ValueError
        if "Properties" in resource and not isinstance(resource["Properties"], dict):
            raise ValueError
        if "Condition" in resource and not isinstance(resource["Condition"], str):
            raise ValueError
        if "Metadata" in resource and not isinstance(resource["Metadata"], dict):
            raise ValueError
        if "DependsOn" in resource:
            dependency = resource["DependsOn"]
            if not isinstance(dependency, str) and not (
                isinstance(dependency, list) and all(isinstance(x, str) for x in dependency)
            ):
                raise ValueError
    for section in ("Parameters", "Mappings", "Conditions", "Outputs", "Metadata", "Rules"):
        if section in template and not isinstance(template[section], dict):
            raise ValueError
    for parameter in template.get("Parameters", {}).values():
        if not isinstance(parameter, dict) or not isinstance(parameter.get("Type"), str):
            raise ValueError
        if "NoEcho" in parameter and type(parameter["NoEcho"]) is not bool:
            raise ValueError
        if "AllowedValues" in parameter and not isinstance(parameter["AllowedValues"], list):
            raise ValueError
    for mapping in template.get("Mappings", {}).values():
        if not isinstance(mapping, dict) or not all(isinstance(v, dict) for v in mapping.values()):
            raise ValueError
    for condition in template.get("Conditions", {}).values():
        if type(condition) not in (bool, dict):
            raise ValueError
    for output in template.get("Outputs", {}).values():
        if not isinstance(output, dict) or "Value" not in output:
            raise ValueError
    if "Transform" in template:
        transform = template["Transform"]
        if not isinstance(transform, str) and not (
            isinstance(transform, list) and all(isinstance(x, str) for x in transform)
        ):
            raise ValueError


def parse_template(text: str) -> dict:
    """Parse bounded JSON/YAML data; never execute transforms or custom YAML objects."""
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError
        depth = count = 0
        for event in yaml.parse(text, Loader=_TemplateLoader):
            count += 1
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if depth > MAX_DEPTH or count > MAX_NODES * 3:
                raise ValueError
        try:
            template = json.loads(
                text, object_pairs_hook=unique_object, parse_constant=reject_constant
            )
        except json.JSONDecodeError:
            template = yaml.load(text, Loader=_TemplateLoader)
        _validate_template(template)
        return template
    except (yaml.YAMLError, ValueError, TypeError, RecursionError, OverflowError):
        raise IngestionError("invalid_request", _MESSAGE) from None


def load_template(path: Path) -> dict:
    """Read at most the template byte limit, suppressing filesystem/source details."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError
        text = data.decode("utf-8")
    except (OSError, ValueError):
        raise IngestionError("invalid_request", _MESSAGE) from None
    return parse_template(text)
