"""Staged, bounded ELBv2 reads with source-local discovery limitations."""

import json
from dataclasses import dataclass, replace
from typing import Any

from ...models.architecture import Coverage, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ..adapter import AdapterContext
from ..detail_execution import (
    DetailCall,
    DetailResponse,
    DetailResult,
    DetailRunner,
    with_problems,
)
from ..operations import Elbv2Operation as Op
from ..requests import AwsAccountConfig
from .identity import valid_arn

_KEYS = {
    Op.DESCRIBE_LOAD_BALANCERS: "LoadBalancers",
    Op.DESCRIBE_TARGET_GROUPS: "TargetGroups",
    Op.DESCRIBE_LISTENERS: "Listeners",
    Op.DESCRIBE_RULES: "Rules",
    Op.DESCRIBE_TARGET_HEALTH: "TargetHealthDescriptions",
    Op.DESCRIBE_TAGS: "TagDescriptions",
}


@dataclass
class Elbv2Inventory:
    results: dict[Op, DetailResult]

    @property
    def coverage(self) -> list[Coverage]:
        return [result.coverage for result in self.results.values()]


def _problem(path: str, reason: str) -> UnresolvedValue:
    return UnresolvedValue(configuration_path=path, reason=reason)


def _prerequisite(*results: DetailResult) -> str | None:
    unavailable = sorted(
        {
            result.coverage.operation
            for result in results
            if result.coverage.status != CoverageStatus.SUCCESS
        }
    )
    if unavailable:
        return f"Discovery was unavailable or incomplete: {', '.join(unavailable)}"
    return None


class _InventoryRunner(DetailRunner):
    """Retain failed request scope to avoid qualifying unrelated sibling stages."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_calls: list[DetailCall] = []

    def _execute(self, client, call, canonical):
        result = super()._execute(client, call, canonical)
        if result.failure is not None:
            with self._lock:
                self.failed_calls.append(call)
        return result


# The stages whose resources are modelled for some types only: the type field, and those types.
_SUPPORTED = {
    Op.DESCRIBE_LOAD_BALANCERS: ("Type", ("application", "network")),
    Op.DESCRIBE_TARGET_GROUPS: ("TargetType", ("instance", "ip", "alb", "lambda")),
}
_LOAD_BALANCER_KINDS = {"application": "app", "network": "net", "gateway": "gwy"}
_TAG_BATCH = 20


def _resource_path(arn: str) -> str:
    return arn.split(":", 5)[5]


def _load_balancer_matches(response: DetailResponse, arn: str) -> bool:
    lb_type = response.value.get("Type")
    kind = _LOAD_BALANCER_KINDS.get(lb_type if isinstance(lb_type, str) else None)
    return kind is None or _resource_path(arn).startswith(f"loadbalancer/{kind}/")


def _listener_matches(response: DetailResponse, arn: str) -> bool:
    parent = response.call.params["LoadBalancerArn"]
    listener = _resource_path(arn).removeprefix("listener/").rsplit("/", 1)[0]
    load_balancer = _resource_path(parent).removeprefix("loadbalancer/")
    return response.value.get("LoadBalancerArn", parent) == parent and listener == load_balancer


def _rule_matches(response: DetailResponse, arn: str) -> bool:
    parent = response.call.params["ListenerArn"]
    rule = _resource_path(arn).removeprefix("listener-rule/").rsplit("/", 1)[0]
    return rule == _resource_path(parent).removeprefix("listener/")


# Checks that a well-formed ARN agrees with its reply's type or the parent it was read under.
_MATCHES = {
    "LoadBalancerArn": _load_balancer_matches,
    "ListenerArn": _listener_matches,
    "RuleArn": _rule_matches,
}


def _recounted(coverage: Coverage, responses: list[DetailResponse]) -> DetailResult:
    """The responses, with the coverage's result count, when it has one, set to their number."""
    fields = coverage.model_dump()
    if fields["result_count"] is not None:
        fields["result_count"] = len(responses)
    return DetailResult(responses, Coverage(**fields))


def _unconflicted(
    candidates: dict[str, list[DetailResponse]], tainted: set[str], field: str
) -> tuple[dict[str, DetailResponse], set[str], list[UnresolvedValue]]:
    """The ARNs whose replies all agree, the conflicting ARNs, and a problem per conflict."""
    safe = {}
    conflicts = set()
    problems = []
    for arn, entries in candidates.items():
        first = entries[0]
        if arn in tainted or any(
            entry.value != first.value or entry.call.params != first.call.params
            for entry in entries[1:]
        ):
            conflicts.add(arn)
            problems.extend(
                _problem(f"{entry.path}.{field}", "Conflicting resource identity")
                for entry in entries
            )
        else:
            safe[arn] = first
    return safe, conflicts, problems


def _well_formed_tags(tags: Any) -> bool:
    return isinstance(tags, list) and all(
        isinstance(tag, dict)
        and isinstance(tag.get("Key"), str)
        and isinstance(tag.get("Value"), str)
        for tag in tags
    )


def _checked_tags(
    responses: list[DetailResponse],
) -> tuple[list[DetailResponse], dict[tuple[str, ...], set[str]], list[UnresolvedValue]]:
    """The well-formed, consistent tag replies, the ARNs each request was answered for, and
    the problems found."""
    problems = []
    returned: dict[tuple[str, ...], set[str]] = {}
    entries: dict[str, list[DetailResponse]] = {}
    for response in responses:
        requested = tuple(response.call.params["ResourceArns"])
        value = response.value
        arn = value.get("ResourceArn") if isinstance(value, dict) else None
        if not isinstance(arn, str) or arn not in requested:
            problems.append(_problem(f"{response.path}.ResourceArn", "Unexpected tag identity"))
            continue
        returned.setdefault(requested, set()).add(arn)
        if not _well_formed_tags(value.get("Tags")):
            problems.append(_problem(f"{response.path}.Tags", "Tag collection is unavailable"))
            continue
        entries.setdefault(arn, []).append(response)
    retained = []
    for group in entries.values():
        if any(entry.value != group[0].value for entry in group[1:]):
            problems.extend(
                _problem(f"{entry.path}.Tags", "Conflicting tag replies") for entry in group
            )
        else:
            retained.extend(group)
    return retained, returned, problems


class _Stages:
    """Each stage's result so far, read through one runner for one account and region."""

    def __init__(self, runner: _InventoryRunner, account_id: str, region: AwsRegion) -> None:
        self.runner = runner
        self.account_id = account_id
        self.region = region
        self.results: dict[Op, DetailResult] = {}

    def run(self, op: Op, calls: list[DetailCall], *upstream: DetailResult) -> DetailResult:
        # Keep whole pages until shape validation: absent keys are not empty inventories.
        raw = self.runner.run(op, calls, prerequisite=_prerequisite(*upstream))
        responses = []
        problems = []
        key = _KEYS[op]
        for response in raw.responses:
            path = f"{response.path}.{key}"
            value = response.value
            if not isinstance(value, dict) or not isinstance(value.get(key), list):
                problems.append(_problem(path, "Required result collection is unavailable"))
                continue
            responses.extend(
                DetailResponse(replace(response.call, result_key=key), item, f"{path}[{index}]")
                for index, item in enumerate(value[key])
            )
        result = with_problems(_recounted(raw.coverage, responses), problems)
        self.results[op] = result
        return result

    def identities(self, op: Op, field: str) -> dict[str, DetailResponse]:
        """The stage's replies with a valid, unconflicted identity, by ARN; the others are
        dropped from the stage's result as problems."""
        result = self.results[op]
        problems = []
        candidates: dict[str, list[DetailResponse]] = {}
        unsafe: set[int] = set()
        tainted: set[str] = set()
        for index, response in enumerate(result.responses):
            value = response.value
            arn = value.get(field) if isinstance(value, dict) else None
            if self._valid(response, arn, field):
                candidates.setdefault(arn, []).append(response)
                continue
            unsafe.add(index)
            if valid_arn(arn, field, self.account_id, self.region):
                tainted.add(arn)
            problems.append(_problem(f"{response.path}.{field}", "Invalid resource identity"))
        safe, conflicts, conflicting = _unconflicted(candidates, tainted, field)
        retained = [
            response
            for index, response in enumerate(result.responses)
            if index not in unsafe and response.value[field] not in conflicts
        ]
        self.results[op] = with_problems(
            _recounted(result.coverage, retained), [*problems, *conflicting]
        )
        return safe

    def _valid(self, response: DetailResponse, arn: Any, field: str) -> bool:
        if not valid_arn(arn, field, self.account_id, self.region):
            return False
        matches = _MATCHES.get(field)
        return matches is None or matches(response, arn)

    def supported(self, op: Op, items: dict[str, DetailResponse]) -> dict[str, DetailResponse]:
        """The items of a modelled type; the others keep their summary, with a problem."""
        field, types = _SUPPORTED[op]
        supported = {arn: item for arn, item in items.items() if item.value.get(field) in types}
        self.results[op] = with_problems(
            self.results[op],
            [
                _problem(f"{item.path}.{field}", "Unsupported resource type; summary retained")
                for arn, item in items.items()
                if arn not in supported
            ],
        )
        return supported

    def rule_upstream(self, alb_arns: set[str]) -> list[DetailResult]:
        """The stages reading rules depends on: the listeners too, unless every application
        load balancer's listeners were read."""
        upstream = [self.results[Op.DESCRIBE_LOAD_BALANCERS]]
        listeners = self.results[Op.DESCRIBE_LISTENERS]
        if alb_arns and (
            self._alb_listeners_unread(alb_arns)
            or listeners.coverage.status == CoverageStatus.FAILED
        ):
            upstream.append(listeners)
        return upstream

    def _alb_listeners_unread(self, alb_arns: set[str]) -> bool:
        # NLB listeners legitimately have no rules, even if their listener reads failed.
        problems = self.results[Op.DESCRIBE_LISTENERS].coverage.unresolved_values
        return any(
            f'"LoadBalancerArn":"{arn}"' in problem.configuration_path
            for problem in problems
            for arn in alb_arns
        ) or any(
            call.operation == Op.DESCRIBE_LISTENERS and call.params["LoadBalancerArn"] in alb_arns
            for call in self.runner.failed_calls
        )

    def tags(self, arns: list[str]) -> None:
        batches = [arns[index : index + _TAG_BATCH] for index in range(0, len(arns), _TAG_BATCH)]
        tags = self.run(
            Op.DESCRIBE_TAGS,
            [DetailCall(Op.DESCRIBE_TAGS, {"ResourceArns": batch}) for batch in batches],
            *(
                self.results[op]
                for op in (
                    Op.DESCRIBE_LOAD_BALANCERS,
                    Op.DESCRIBE_TARGET_GROUPS,
                    Op.DESCRIBE_LISTENERS,
                    Op.DESCRIBE_RULES,
                )
            ),
        )
        retained, returned, problems = _checked_tags(tags.responses)
        # Missing replies are source-local limitations, never fabricated empty tag sets.
        for batch in batches:
            canonical = json.dumps({"ResourceArns": batch}, sort_keys=True, separators=(",", ":"))
            problems.extend(
                _problem(
                    f"Request[{canonical}].Page[0].TagDescriptions[ResourceArn={arn}]",
                    "Requested resource tag reply is unavailable",
                )
                for arn in sorted(set(batch) - returned.get(tuple(batch), set()))
            )
        result = _recounted(tags.coverage, retained)
        # A failed batch has no successful reads; preserve FAILED rather than claiming a
        # partial read.
        if tags.coverage.status == CoverageStatus.FAILED:
            fields = result.coverage.model_dump()
            fields["unresolved_values"] = [*tags.coverage.unresolved_values, *problems]
            self.results[Op.DESCRIBE_TAGS] = DetailResult(retained, Coverage(**fields))
        else:
            self.results[Op.DESCRIBE_TAGS] = with_problems(result, problems)


def collect_inventory(
    session: Any, account: AwsAccountConfig, context: AdapterContext, region: AwsRegion
) -> Elbv2Inventory:
    runner = _InventoryRunner(
        session,
        account,
        context,
        service=AwsService.ELASTIC_LOAD_BALANCING,
        region=region,
        client_name="elbv2",
        pagination="elbv2",
    )
    stages = _Stages(runner, account.expected_account_id, region)
    stages.run(Op.DESCRIBE_LOAD_BALANCERS, [DetailCall(Op.DESCRIBE_LOAD_BALANCERS, paginate=True)])
    stages.run(Op.DESCRIBE_TARGET_GROUPS, [DetailCall(Op.DESCRIBE_TARGET_GROUPS, paginate=True)])
    lbs = stages.supported(
        Op.DESCRIBE_LOAD_BALANCERS,
        stages.identities(Op.DESCRIBE_LOAD_BALANCERS, "LoadBalancerArn"),
    )
    groups = stages.supported(
        Op.DESCRIBE_TARGET_GROUPS, stages.identities(Op.DESCRIBE_TARGET_GROUPS, "TargetGroupArn")
    )
    stages.run(
        Op.DESCRIBE_LISTENERS,
        [
            DetailCall(Op.DESCRIBE_LISTENERS, {"LoadBalancerArn": arn}, paginate=True)
            for arn in sorted(lbs)
        ],
        stages.results[Op.DESCRIBE_LOAD_BALANCERS],
    )
    listeners = stages.identities(Op.DESCRIBE_LISTENERS, "ListenerArn")
    alb_arns = {arn for arn, response in lbs.items() if response.value["Type"] == "application"}
    stages.run(
        Op.DESCRIBE_RULES,
        [
            DetailCall(Op.DESCRIBE_RULES, {"ListenerArn": arn}, paginate=True)
            for arn, response in sorted(listeners.items())
            if response.call.params["LoadBalancerArn"] in alb_arns
        ],
        *stages.rule_upstream(alb_arns),
    )
    rules = stages.identities(Op.DESCRIBE_RULES, "RuleArn")
    stages.run(
        Op.DESCRIBE_TARGET_HEALTH,
        [DetailCall(Op.DESCRIBE_TARGET_HEALTH, {"TargetGroupArn": arn}) for arn in sorted(groups)],
        stages.results[Op.DESCRIBE_TARGET_GROUPS],
    )
    stages.tags(sorted(set(lbs) | set(listeners) | set(rules) | set(groups)))
    return Elbv2Inventory(stages.results)
