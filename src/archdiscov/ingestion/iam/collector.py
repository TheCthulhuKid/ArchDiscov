"""Staged IAM inventory and discovery of only referenced managed policies."""

import re
from dataclasses import dataclass
from typing import Any

from ...models.enums import CoverageStatus
from ..adapter import AdapterContext
from ..operations import IamOperation as Op
from ..requests import AwsAccountConfig
from .execution import IamCall, IamResult, IamRunner

_POLICY_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(?:[0-9]{12}|aws):policy/[^\s*?]+$")


@dataclass
class IamInventory:
    results: dict[Op, IamResult]


def _prerequisite(*results: IamResult) -> str | None:
    unavailable = sorted(
        result.coverage.operation
        for result in results
        if result.coverage.status != CoverageStatus.SUCCESS
    )
    if unavailable:
        return f"Discovery was unavailable or incomplete: {', '.join(unavailable)}"
    return None


def collect_inventory(
    session: Any, account: AwsAccountConfig, context: AdapterContext
) -> IamInventory:
    runner = IamRunner(session, account, context)
    results: dict[Op, IamResult] = {}

    def run(op: Op, calls: list[IamCall], *upstream: IamResult) -> IamResult:
        result = runner.run(op, calls, prerequisite=_prerequisite(*upstream))
        results[op] = result
        return result

    roles = run(Op.LIST_ROLES, [IamCall(Op.LIST_ROLES, result_key="Roles", paginate=True)])
    run(
        Op.LIST_INSTANCE_PROFILES,
        [IamCall(Op.LIST_INSTANCE_PROFILES, result_key="InstanceProfiles", paginate=True)],
    )
    names = sorted(
        {
            r.value["RoleName"]
            for r in roles.responses
            if isinstance(r.value, dict)
            and isinstance(r.value.get("RoleName"), str)
            and r.value["RoleName"].strip()
        }
    )
    details = run(Op.GET_ROLE, [IamCall(Op.GET_ROLE, {"RoleName": n}) for n in names], roles)
    attached = run(
        Op.LIST_ATTACHED_ROLE_POLICIES,
        [
            IamCall(Op.LIST_ATTACHED_ROLE_POLICIES, {"RoleName": n}, "AttachedPolicies", True)
            for n in names
        ],
        roles,
    )
    inline_names = run(
        Op.LIST_ROLE_POLICIES,
        [IamCall(Op.LIST_ROLE_POLICIES, {"RoleName": n}, "PolicyNames", True) for n in names],
        roles,
    )
    inline_calls = {
        (r.call.params["RoleName"], r.value)
        for r in inline_names.responses
        if isinstance(r.value, str) and r.value.strip()
    }
    run(
        Op.GET_ROLE_POLICY,
        [
            IamCall(Op.GET_ROLE_POLICY, {"RoleName": role, "PolicyName": policy})
            for role, policy in sorted(inline_calls)
        ],
        inline_names,
    )
    arns = set()
    for response in attached.responses:
        if isinstance(response.value, dict):
            arns.add(response.value.get("PolicyArn"))
    for response in [*roles.responses, *details.responses]:
        value = response.value
        if not isinstance(value, dict):
            continue
        role = value.get("Role", value)
        boundary = role.get("PermissionsBoundary") or {}
        if isinstance(boundary, dict):
            arns.add(boundary.get("PermissionsBoundaryArn"))
    safe_arns = sorted(a for a in arns if isinstance(a, str) and _POLICY_ARN.fullmatch(a))
    policies = run(
        Op.GET_POLICY,
        [IamCall(Op.GET_POLICY, {"PolicyArn": arn}) for arn in safe_arns],
        roles,
        details,
        attached,
    )
    versions = set()
    for response in policies.responses:
        policy = response.value.get("Policy", {})
        version = policy.get("DefaultVersionId")
        if isinstance(version, str) and re.fullmatch(r"v[0-9]+", version):
            versions.add((response.call.params["PolicyArn"], version))
    run(
        Op.GET_POLICY_VERSION,
        [
            IamCall(Op.GET_POLICY_VERSION, {"PolicyArn": arn, "VersionId": version})
            for arn, version in sorted(versions)
        ],
        policies,
    )
    return IamInventory(results)
