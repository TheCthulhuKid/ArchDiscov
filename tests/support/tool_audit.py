"""The schema audit every tool declaration must pass.

A declaration is closed and bounded, and none of its inputs lets a model choose data, reach a
network, run commands, or change state.
"""

import json

from archdiscov.assistant.tools.registry import ToolDefinition

# Words that would let a model choose data, reach a network, run commands, or change state.
FORBIDDEN_WORDS = {
    "path", "paths", "file", "files", "filename", "dir", "directory", "folder",
    "snapshot", "snapshots", "url", "uri", "host", "hostname", "endpoint", "address",
    "command", "cmd", "shell", "exec", "execute", "script", "code",
    "write", "save", "output", "delete", "remove", "upload", "download", "persist",
}  # fmt: skip
FORBIDDEN_FORMATS = {"uri", "uri-reference", "iri", "hostname", "path", "binary"}


def _properties(schema: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Every property with its schema, through objects, arrays, and anyOf branches."""
    found = []
    for name, child in schema.get("properties", {}).items():
        found.append((prefix + name, child))
        found.extend(_properties(child, f"{prefix}{name}."))
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(key, []):
            found.extend(_properties(branch, prefix))
    if isinstance(schema.get("items"), dict):
        found.extend(_properties(schema["items"], prefix))
    return found


def _leaves(schema: dict) -> list[dict]:
    """Typed leaf schemas, expanding anyOf branches and array items."""
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        return [leaf for branch in branches for leaf in _leaves(branch)]
    if schema.get("type") == "array":
        return [schema, *_leaves(schema.get("items", {}))]
    return [schema]


def assert_no_write_network_shell_or_path_inputs(tool: ToolDefinition) -> None:
    schema = tool.declaration.input_schema
    assert schema["type"] == "object"
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    for name, child in _properties(schema):
        words = set(name.lower().replace(".", "_").split("_"))
        assert not words & FORBIDDEN_WORDS, (tool.name, name)
        for leaf in _leaves(child):
            assert leaf.get("format") not in FORBIDDEN_FORMATS, (tool.name, name)


def assert_bounded_and_closed(tool: ToolDefinition) -> None:
    schema = tool.declaration.input_schema
    objects = [schema] + [
        child for _, child in _properties(schema) if child.get("type") == "object"
    ]
    for item in objects:
        assert item.get("additionalProperties") is False, tool.name
    for name, child in _properties(schema):
        for leaf in _leaves(child):
            kind = leaf.get("type")
            if kind == "string":
                assert "enum" in leaf or "const" in leaf or "maxLength" in leaf, (tool.name, name)
            elif kind == "integer":
                assert "minimum" in leaf and "maximum" in leaf, (tool.name, name)
            elif kind == "array":
                assert "maxItems" in leaf, (tool.name, name)
            else:
                assert kind in {"null", "boolean", "object", None}, (tool.name, name, kind)
