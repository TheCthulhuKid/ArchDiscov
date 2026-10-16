"""List Lambda functions, then read configuration only (never GetFunction)."""

from typing import Any
from uuid import UUID

from ...models.architecture import Coverage, Evidence, ResourceIdentity, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus, SourceType
from ...models.resources.functions import (
    SOURCE_FIELDS,
    LambdaFunction,
    safe_configuration,
    valid_source_field,
)
from ..adapter import AdapterContext
from ..configuration_common import ServiceCollection
from ..coverage_updates import with_unresolved_value
from ..detail_execution import DetailCall, DetailRunner
from ..evidence_builder import EvidenceBuilder
from ..operations import LambdaOperation as Op
from ..requests import AwsAccountConfig


class _Normalizer(EvidenceBuilder):
    def __init__(self, snapshot_id, account_id, region):
        super().__init__(snapshot_id, account_id, region, AwsService.LAMBDA)
        self.resources: dict[str, LambdaFunction] = {}
        self.coverage: dict[str, Coverage] = {}

    def _evidence(self, key, resource_id, operation, path, value):
        # Whole records have already been projected; scalar facts passed strict validation.
        safe, paths = safe_configuration(value) if isinstance(value, dict) else (value, [])
        self.evidence[key] = Evidence(
            snapshot_id=self.snapshot_id,
            resource_id=resource_id,
            source=SourceType.AWS,
            locator=f"lambda:{operation}",
            configuration_path=path,
            value=safe,
            redacted_paths=paths,
        )
        return key

    def issue(self, op, path, resource=None):
        coverage = self.coverage[op]
        unresolved = UnresolvedValue(
            resource_id=resource.id if resource is not None else None,
            configuration_path=path,
            reason="Missing, malformed, or unsupported Lambda value",
        )
        self.coverage[op] = with_unresolved_value(coverage, unresolved)

    def normalize(self, response, *, detail=False):
        op, path, raw = response.call.operation, response.path, response.value
        if not isinstance(raw, dict) or (not detail and path.endswith(".Functions")):
            self.issue(op, path)
            return
        arn = response.call.params.get("FunctionName") if detail else raw.get("FunctionArn")
        if not valid_source_field("FunctionArn", arn):
            self.issue(op, f"{path}.FunctionArn")
            return
        resource = self.resources.get(arn)
        if resource is None:
            resource = LambdaFunction(
                identity=ResourceIdentity(
                    account_id=self.account_id,
                    region=self.region,
                    service=AwsService.LAMBDA,
                    physical_id=arn,
                ),
                arn=arn,
            )
            self.resources[arn] = resource
        if detail and raw.get("FunctionArn") != arn:
            self.issue(op, f"{path}.FunctionArn", resource)
            return
        safe, redactions = safe_configuration(raw)
        # Project BEFORE creating whole-source evidence, including SDK extras and diagnostics.
        eid = self.source_evidence(resource, op, path, safe)
        if redactions:
            self.evidence[eid] = Evidence(
                **{**self.evidence[eid].model_dump(), "redacted_paths": redactions}
            )
        resource.evidence_ids.append(eid)
        for field, key in SOURCE_FIELDS.items():
            source, source_path = raw, path
            if key in {"VpcId", "SubnetIds", "SecurityGroupIds"}:
                if "VpcConfig" not in raw:
                    continue
                source, source_path = raw["VpcConfig"], f"{path}.VpcConfig"
                if not isinstance(source, dict):
                    continue
            if key not in source:
                continue
            value = source[key]
            if not valid_source_field(key, value):
                self.issue(op, f"{source_path}.{key}", resource)
                if field != "arn":
                    setattr(resource, field, None)
                    self.clear_fact(resource, field)
                continue
            setattr(resource, field, value)
            # Cite the current value only; whole records retain the earlier observation.
            self.clear_fact(resource, field)
            self.cite(resource, f"/{field}", op, f"{source_path}.{key}", value)
            if isinstance(value, list):
                for index, child in enumerate(value):
                    self.cite(
                        resource, f"/{field}/{index}", op, f"{source_path}.{key}[{index}]", child
                    )
        if "VpcConfig" in raw and not isinstance(raw["VpcConfig"], dict):
            self.issue(op, f"{path}.VpcConfig", resource)
            for field in ("vpc_id", "subnet_ids", "security_group_ids"):
                setattr(resource, field, None)
                self.clear_fact(resource, field)
        resource.configuration = safe
        resource.fact_evidence["/configuration"] = [eid]

    @staticmethod
    def clear_fact(resource, field):
        for pointer in list(resource.fact_evidence):
            if pointer == f"/{field}" or pointer.startswith(f"/{field}/"):
                del resource.fact_evidence[pointer]


def collect_lambda(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    normalizer = _Normalizer(snapshot_id, account_id, region)
    runner = DetailRunner(
        session,
        account,
        context,
        service=AwsService.LAMBDA,
        region=region,
        client_name="lambda",
        pagination="lambda",
    )
    listed = runner.run(
        Op.LIST_FUNCTIONS,
        [DetailCall(Op.LIST_FUNCTIONS, result_key="Functions", paginate=True)],
    )
    normalizer.coverage[Op.LIST_FUNCTIONS] = listed.coverage
    for response in listed.responses:
        normalizer.normalize(response)
    prerequisite = None
    if normalizer.coverage[Op.LIST_FUNCTIONS].status != CoverageStatus.SUCCESS:
        prerequisite = "Discovery was unavailable or incomplete: ListFunctions"
    details = runner.run(
        Op.GET_FUNCTION_CONFIGURATION,
        [
            DetailCall(Op.GET_FUNCTION_CONFIGURATION, {"FunctionName": arn})
            for arn in sorted(normalizer.resources)
        ],
        prerequisite=prerequisite,
    )
    normalizer.coverage[Op.GET_FUNCTION_CONFIGURATION] = details.coverage
    for response in details.responses:
        normalizer.normalize(response, detail=True)
    return ServiceCollection(
        resources=list(normalizer.resources.values()),
        evidence=normalizer.evidence,
        coverage=list(normalizer.coverage.values()),
    )
