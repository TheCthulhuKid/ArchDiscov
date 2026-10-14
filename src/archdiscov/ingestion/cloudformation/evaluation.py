import re
from dataclasses import dataclass, field
from typing import Any

from ...models.json_pointer import escape_pointer_token
from .evaluation_limits import MAX_BYTES, MAX_DEPTH, MAX_NODES, validate_json
from .intrinsics import (
    INTRINSIC_ARITIES,
    IntrinsicLimitExceeded,
    InvalidIntrinsic,
    evaluate_pure,
)
from .issue_codes import EvaluationIssueCode

_UNKNOWN = object()
_OMIT = object()
_REDACTED = "[REDACTED]"
_VARIABLE = re.compile(r"\$\{([^{}]+)\}")


@dataclass(frozen=True)
class Issue:
    path: str
    reason: EvaluationIssueCode


@dataclass(frozen=True)
class Reference:
    logical_id: str
    path: str
    attribute: str | None = None
    external: bool = False


@dataclass
class Evaluation:
    value: Any
    issues: list[Issue] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    redacted_paths: list[str] = field(default_factory=list)
    resolved: bool = True
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.resolved


def _pointer(path: str, key: str | int) -> str:
    return path + "/" + escape_pointer_token(str(key))


def _is_intrinsic(value: Any) -> bool:
    return isinstance(value, dict) and (
        any(key == "Ref" or key.startswith("Fn::") for key in value)
        or len(value) == 1
        and "Condition" in value
    )


class _LimitExceeded(Exception):
    pass


class _Evaluator:
    def __init__(
        self, parameters: dict, resources: set[str], pseudo: dict, mappings: dict, conditions: dict
    ):
        self.parameters = parameters
        self.resources = resources
        self.pseudo = pseudo
        self.mappings = mappings
        self.conditions = conditions
        self.result = Evaluation(None)
        self.nodes = 0
        self.produced = 0
        self.condition_stack: set[str] = set()
        self.redacted_set: set[str] = set()

    def unknown(self, path: str, reason: EvaluationIssueCode) -> Any:
        self.result.issues.append(Issue(path, reason))
        self.result.resolved = False
        return _UNKNOWN

    def redacted(self, path: str) -> None:
        if path not in self.redacted_set:
            self.redacted_set.add(path)
            self.result.redacted_paths.append(path)

    def contains_redacted(self, value: Any) -> bool:
        if isinstance(value, str):
            return _REDACTED in value
        if isinstance(value, list):
            return any(self.contains_redacted(v) for v in value)
        if isinstance(value, dict):
            return any(self.contains_redacted(v) for v in value.values())
        return False

    def derived(self, values: Any, path: str) -> bool:
        if self.contains_redacted(values):
            self.redacted(path)
            self.unknown(path, EvaluationIssueCode.REDACTED_INPUT)
            return False
        return True

    def budget(self, value: Any, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _LimitExceeded
        if isinstance(value, str):
            self.produced += len(value.encode("utf-8"))
            if self.produced > MAX_BYTES:
                raise _LimitExceeded

    def literal(self, value: Any, path: str, depth: int) -> Any:
        self.budget(value, depth)
        if isinstance(value, dict):
            return {
                key: self.literal(v, _pointer(path, key), depth + 1) for key, v in value.items()
            }
        if isinstance(value, list):
            return [self.literal(v, _pointer(path, i), depth + 1) for i, v in enumerate(value)]
        if isinstance(value, str) and _REDACTED in value:
            self.redacted(path)
        return value

    def ref(self, name: Any, path: str, depth: int) -> Any:
        if not isinstance(name, str) or not name:
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        if not self.derived(name, path):
            return _UNKNOWN
        if name == "AWS::NoValue":
            return _OMIT
        if name in self.resources:
            self.result.references.append(Reference(name, path))
            return self.unknown(path, EvaluationIssueCode.RESOURCE_VALUE_UNAVAILABLE)
        if name in self.parameters:
            return self.literal(self.parameters[name], path, depth + 1)
        if name in self.pseudo:
            return self.literal(self.pseudo[name], path, depth + 1)
        return self.unknown(path, EvaluationIssueCode.REFERENCE_UNAVAILABLE)

    def getatt(self, argument: Any, path: str) -> Any:
        if isinstance(argument, str):
            argument = argument.split(".", 1)
        if not (
            isinstance(argument, list)
            and len(argument) == 2
            and all(isinstance(x, str) and x for x in argument)
        ):
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        if not self.derived(argument, path):
            return _UNKNOWN
        name, attribute = argument
        if name in self.resources:
            self.result.references.append(Reference(name, path, attribute))
            return self.unknown(path, EvaluationIssueCode.RESOURCE_VALUE_UNAVAILABLE)
        return self.unknown(path, EvaluationIssueCode.REFERENCE_UNAVAILABLE)

    def condition(self, name: Any, path: str, depth: int) -> Any:
        if not isinstance(name, str) or name not in self.conditions:
            return self.unknown(path, EvaluationIssueCode.CONDITION_UNAVAILABLE)
        if name in self.condition_stack:
            return self.unknown(path, EvaluationIssueCode.CONDITION_CYCLE)
        self.condition_stack.add(name)
        try:
            value = self.visit(self.conditions[name], path, depth + 1)
        finally:
            self.condition_stack.remove(name)
        if value is _UNKNOWN:
            return value
        if type(value) is not bool:
            return self.unknown(path, EvaluationIssueCode.INVALID_CONDITION)
        return value

    def sub(self, argument: Any, path: str, depth: int) -> Any:
        variables = {}
        if isinstance(argument, str):
            template = argument
        elif (
            isinstance(argument, list)
            and len(argument) == 2
            and isinstance(argument[0], str)
            and isinstance(argument[1], dict)
        ):
            template, variables = argument
        else:
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        if not self.derived(template, path):
            return _UNKNOWN
        remaining = _VARIABLE.sub("", template)
        if "${" in remaining:
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        failed = False
        cache = {}

        def replace(match: re.Match) -> str:
            nonlocal failed
            name = match.group(1)
            if name.startswith("!"):
                return "${" + name[1:] + "}"
            if name in cache:
                self.budget(cache[name], depth)
                return cache[name]
            if name in variables:
                value = self.visit(variables[name], path, depth + 1)
            elif "." in name:
                value = self.getatt(name, path)
            else:
                value = self.ref(name, path, depth + 1)
            if not self.derived(value, path):
                value = _UNKNOWN
            if value is _UNKNOWN or value is _OMIT:
                if value is _OMIT:
                    self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
                failed = True
                replacement = ""
            elif type(value) not in (str, int, float, bool):
                self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
                failed = True
                replacement = ""
            else:
                replacement = str(value).lower() if type(value) is bool else str(value)
            cache[name] = replacement
            self.budget(replacement, depth)
            return replacement

        value = _VARIABLE.sub(replace, template)
        if failed:
            return _UNKNOWN
        self.budget(value, depth)
        return value

    def intrinsic(self, name: str, argument: Any, path: str, depth: int) -> Any:
        if name == "Ref":
            return self.ref(argument, path, depth)
        if name == "Fn::GetAtt":
            return self.getatt(argument, path)
        if name == "Condition":
            return self.condition(argument, path, depth)
        if name == "Fn::Sub":
            return self.sub(argument, path, depth)
        if name == "Fn::If":
            if not isinstance(argument, list) or len(argument) != 3:
                return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
            condition = self.condition(argument[0], path, depth + 1)
            if condition is _UNKNOWN:
                return _UNKNOWN
            return self.visit(argument[1] if condition else argument[2], path, depth + 1)
        if name == "Fn::ImportValue":
            value = self.visit(argument, path, depth + 1)
            if not self.derived(value, path):
                return _UNKNOWN
            return self.unknown(path, EvaluationIssueCode.IMPORT_UNAVAILABLE)
        if name not in INTRINSIC_ARITIES:
            return self.unknown(path, EvaluationIssueCode.UNSUPPORTED_INTRINSIC)
        low, high = INTRINSIC_ARITIES[name]
        if not isinstance(argument, list) or not low <= len(argument) <= high:
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        values = [self.visit(v, path, depth + 1) for v in argument]
        if not self.derived(values, path):
            return _UNKNOWN
        if any(v is _UNKNOWN for v in values):
            return _UNKNOWN
        if any(v is _OMIT for v in values):
            return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        if name == "Fn::FindInMap":
            value = self.find_in_map(values, path, depth)
        else:
            try:
                value = evaluate_pure(name, values)
            except InvalidIntrinsic:
                return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
        if value is not _UNKNOWN:
            self.budget(value, depth)
        return value

    def find_in_map(self, values: list, path: str, depth: int) -> Any:
        if all(isinstance(v, str) for v in values):
            mapping, top, key = values
            item = self.mappings.get(mapping, {})
            if isinstance(item, dict):
                item = item.get(top, {})
                if isinstance(item, dict) and key in item:
                    return self.literal(item[key], path, depth + 1)
            return self.unknown(path, EvaluationIssueCode.MAPPING_UNAVAILABLE)
        return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)

    def source_leaves(self, value: Any, path: str, source: str) -> None:
        self.budget(value, 0)
        if isinstance(value, dict) and value:
            for key, child in value.items():
                self.source_leaves(child, _pointer(path, key), source)
        elif isinstance(value, list) and value:
            for index, child in enumerate(value):
                self.source_leaves(child, _pointer(path, index), source)
        else:
            self.result.sources[path] = source

    def visit(self, value: Any, path: str, depth: int) -> Any:
        parent_sources = self.result.sources
        self.result.sources = {}
        issue_start = len(self.result.issues)
        reference_start = len(self.result.references)
        redacted_start = len(self.result.redacted_paths)
        intrinsic = _is_intrinsic(value)
        try:
            result = self._visit(value, path, depth)
            if intrinsic:
                self.result.issues[issue_start:] = [
                    Issue(path, issue.reason) for issue in self.result.issues[issue_start:]
                ]
                self.result.references[reference_start:] = [
                    Reference(ref.logical_id, path, ref.attribute, ref.external)
                    for ref in self.result.references[reference_start:]
                ]
                if len(self.result.redacted_paths) > redacted_start:
                    removed = self.result.redacted_paths[redacted_start:]
                    del self.result.redacted_paths[redacted_start:]
                    self.redacted_set.difference_update(removed)
                    self.redacted(path)
            if result is _UNKNOWN or result is _OMIT:
                self.result.sources.clear()
            elif intrinsic:
                self.result.sources.clear()
                self.source_leaves(result, path, path)
            elif not isinstance(result, (dict, list)) or not result:
                self.result.sources[path] = path
            parent_sources.update(self.result.sources)
        finally:
            self.result.sources = parent_sources
        return result

    def _visit(self, value: Any, path: str, depth: int) -> Any:
        self.budget(value, depth)
        if isinstance(value, dict):
            if _is_intrinsic(value):
                if len(value) != 1:
                    return self.unknown(path, EvaluationIssueCode.INVALID_INTRINSIC)
                name, argument = next(iter(value.items()))
                return self.intrinsic(name, argument, path, depth)
            result = {}
            for key, child in value.items():
                item = self.visit(child, _pointer(path, key), depth + 1)
                if item is not _UNKNOWN and item is not _OMIT:
                    result[key] = item
            return result
        if isinstance(value, list):
            issue_count = len(self.result.issues)
            items = [self.visit(v, _pointer(path, i), depth + 1) for i, v in enumerate(value)]
            if any(v is _UNKNOWN for v in items) or len(self.result.issues) != issue_count:
                return _UNKNOWN
            result = []
            indexes = {}
            for index, item in enumerate(items):
                if item is _OMIT:
                    continue
                indexes[str(index)] = str(len(result))
                result.append(item)
            remapped = {}
            for key, source in self.result.sources.items():
                index, separator, suffix = key[len(path) + 1 :].partition("/")
                if index in indexes:
                    remapped[path + "/" + indexes[index] + separator + suffix] = source
            self.result.sources = remapped
            return result
        if isinstance(value, str) and _REDACTED in value:
            self.redacted(path)
        return value


def evaluate(
    value: Any,
    *,
    parameters: dict,
    resources: set[str],
    pseudo: dict,
    mappings: dict | None = None,
    conditions: dict | None = None,
) -> Evaluation:
    """Evaluate offline, retaining only known fields and never inventing physical IDs.

    Paths are relative JSON pointers. Unknown scalar/root values become None, distinct
    from explicit null via resolved/known. Dictionaries may contain retained fields even
    when unresolved. Unknown list elements invalidate the entire list; NoValue instead
    removes the field/element without an issue. Only the selected If branch is evaluated.
    sources maps output leaf pointers to source pointers, stopping at the outermost
    intrinsic expression boundary, and preserves original indexes after NoValue removal.
    """
    mappings = {} if mappings is None else mappings
    conditions = {} if conditions is None else conditions
    evaluator = _Evaluator(parameters, resources, pseudo, mappings, conditions)
    try:
        for context in (parameters, pseudo, mappings, conditions):
            if type(context) is not dict:
                raise ValueError
            validate_json(context)
        if (
            not isinstance(resources, (set, frozenset))
            or not all(type(name) is str for name in resources)
            or len(resources) > MAX_NODES
        ):
            raise ValueError
        validate_json(value)
        result = evaluator.visit(value, "", 0)
        if result is not _UNKNOWN and result is not _OMIT:
            validate_json(result)
            evaluator.result.value = result
    except (
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        _LimitExceeded,
        IntrinsicLimitExceeded,
    ):
        evaluator.result.value = None
        evaluator.result.sources.clear()
        evaluator.unknown("", EvaluationIssueCode.EVALUATION_LIMIT_OR_INVALID_SHAPE)
    return evaluator.result
