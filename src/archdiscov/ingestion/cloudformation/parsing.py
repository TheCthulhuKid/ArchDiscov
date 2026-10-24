import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from yaml.events import CollectionEndEvent, CollectionStartEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from ...core.strict_json import read_bounded, reject_constant, unique_object
from ..adapter import IngestionError
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


def _strings(value: Any) -> bool:
    """A string or a list of strings, as `DependsOn` and `Transform` take."""
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _optional(item: dict, key: str, valid: Callable[[Any], bool]) -> bool:
    return key not in item or valid(item[key])


def _valid_resource(name: Any, resource: Any) -> bool:
    return (
        bool(name)
        and isinstance(resource, dict)
        and isinstance(resource.get("Type"), str)
        and bool(resource["Type"].strip())
        and _optional(resource, "Properties", _is_dict)
        and _optional(resource, "Condition", lambda value: isinstance(value, str))
        and _optional(resource, "Metadata", _is_dict)
        and _optional(resource, "DependsOn", _strings)
    )


def _valid_parameter(parameter: Any) -> bool:
    return (
        isinstance(parameter, dict)
        and isinstance(parameter.get("Type"), str)
        and _optional(parameter, "NoEcho", lambda value: type(value) is bool)
        and _optional(parameter, "AllowedValues", lambda value: isinstance(value, list))
    )


def _valid_mapping(mapping: Any) -> bool:
    return isinstance(mapping, dict) and all(isinstance(item, dict) for item in mapping.values())


_SECTIONS = ("Parameters", "Mappings", "Conditions", "Outputs", "Metadata", "Rules")
# The check every entry of these sections must pass.
_SECTION_ENTRIES: dict[str, Callable[[Any], bool]] = {
    "Parameters": _valid_parameter,
    "Mappings": _valid_mapping,
    "Conditions": lambda condition: type(condition) in (bool, dict),
    "Outputs": lambda output: isinstance(output, dict) and "Value" in output,
}


def _valid_template(template: Any) -> bool:
    if not isinstance(template, dict) or not isinstance(template.get("Resources"), dict):
        return False
    return (
        template.get("AWSTemplateFormatVersion", "2010-09-09") == "2010-09-09"
        and all(_valid_resource(name, item) for name, item in template["Resources"].items())
        and all(_optional(template, section, _is_dict) for section in _SECTIONS)
        and all(
            valid(entry)
            for section, valid in _SECTION_ENTRIES.items()
            for entry in template.get(section, {}).values()
        )
        and _optional(template, "Transform", _strings)
    )


def _validate_template(template: Any) -> None:
    validate_json(template)
    if not _valid_template(template):
        raise ValueError


def parse_template(text: str) -> dict:
    """Parse bounded JSON/YAML data; never execute transforms or custom YAML objects."""
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=_TemplateLoader), start=1):
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
        text = read_bounded(path, MAX_BYTES).decode("utf-8")
    except (OSError, ValueError):
        raise IngestionError("invalid_request", _MESSAGE) from None
    return parse_template(text)
