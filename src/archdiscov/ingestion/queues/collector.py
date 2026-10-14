"""Bounded, evidence-backed SQS configuration collection."""

import json
from typing import Any
from uuid import UUID

from ...models.architecture import Coverage, Evidence, UnknownResource, UnresolvedValue
from ...models.enums import (
    AwsRegion,
    AwsService,
    CoverageStatus,
    EvidenceValueStatus,
    ResourceType,
    SourceType,
)
from ...models.iam import sanitize_document
from ...models.json_pointer import escape_pointer_token
from ...models.queues import SqsQueue, safe_policy
from ...models.resource_base import Resource, ResourceIdentity
from ..adapter import AdapterContext
from ..configuration_common import ServiceCollection
from ..detail_execution import DetailCall, DetailRunner
from ..evidence_builder import EvidenceBuilder
from ..operations import SqsOperation as Op
from ..requests import AwsAccountConfig
from .values import (
    BOOLEAN_FIELDS,
    ENUM_FIELDS,
    INTEGER_FIELDS,
    integer,
    kms_key,
    queue_arn,
    queue_url,
    redrive,
    redrive_allow,
)


class _Collector(EvidenceBuilder):
    def __init__(self, session, account, context, snapshot_id, account_id, region):
        super().__init__(snapshot_id, account_id, region, AwsService.SQS)
        self.runner = DetailRunner(
            session,
            account,
            context,
            service=AwsService.SQS,
            region=region,
            client_name="sqs",
            pagination="sqs",
        )
        self.coverage = {}
        self.resources = []

    def issue(self, op, path, resource=None):
        reason = "Configuration value unavailable or malformed"
        coverage = self.coverage[op]
        fields = coverage.model_dump()
        fields["unresolved_values"].append(
            UnresolvedValue(
                resource_id=resource.id if resource else None,
                configuration_path=path,
                reason=reason,
            )
        )
        if coverage.status == CoverageStatus.SUCCESS:
            fields["status"] = CoverageStatus.INCOMPLETE
        self.coverage[op] = Coverage(**fields)
        if resource:
            resource.attributes.setdefault("limitations", []).append(f"{op}: {path}: {reason}")
            key = f"sqs/{self.region}/{op}/{resource.identity.physical_id}#{path}"
            self.evidence[key] = Evidence(
                snapshot_id=self.snapshot_id,
                resource_id=resource.id,
                source=SourceType.AWS,
                locator=f"sqs:{op}",
                configuration_path=path,
                value_status=EvidenceValueStatus.OMITTED,
                limitation=reason,
            )
            resource.evidence_ids.append(key)

    def run(self, op, calls, upstream=None):
        prerequisite = None
        if upstream and self.coverage[upstream].status != CoverageStatus.SUCCESS:
            prerequisite = f"Discovery was unavailable or incomplete: {upstream}"
        result = self.runner.run(op, calls, prerequisite=prerequisite)
        self.coverage[op] = result.coverage
        return result.responses

    def whole(self, resource, op, path, value):
        key = self.source_evidence(resource, op, path, value)
        resource.evidence_ids.append(key)
        return key

    def redactions(self, evidence_ids, paths, prefix=""):
        for evidence_id in evidence_ids:
            evidence = self.evidence[evidence_id]
            evidence.redacted_paths = sorted(
                {
                    *evidence.redacted_paths,
                    *(f"{prefix}{path}" for path in paths),
                }
            )
            if evidence.redacted_paths:
                evidence.value_status = EvidenceValueStatus.REDACTED

    def attributes(self, resource, response):
        op = Op.GET_QUEUE_ATTRIBUTES
        path = f"{response.path}.Attributes"
        item = response.value.get("Attributes") if isinstance(response.value, dict) else None
        if not isinstance(item, dict):
            self.issue(op, path, resource)
            return
        safe = {}
        fields = {}
        facts = {}
        for key, value in item.items():
            field = None
            normalized = None
            if key == "QueueArn":
                normalized = value if queue_arn(value) else None
                field = "arn"
            elif key in INTEGER_FIELDS:
                field, minimum, maximum = INTEGER_FIELDS[key]
                normalized = integer(value, minimum, maximum)
            elif key in BOOLEAN_FIELDS:
                field = BOOLEAN_FIELDS[key]
                if isinstance(value, str):
                    normalized = {"true": True, "false": False}.get(value)
            elif key in ENUM_FIELDS:
                normalized = value if isinstance(value, str) and value in ENUM_FIELDS[key] else None
            elif key == "KmsMasterKeyId":
                normalized = value if kms_key(value) else None
                field = "kms_master_key_id"
            elif key == "Policy":
                normalized = safe_policy(value)
                field = "policy"
            elif key == "RedrivePolicy":
                normalized = redrive(value)
            elif key == "RedriveAllowPolicy":
                normalized = redrive_allow(value)
                field = "redrive_allow_policy"
            else:
                continue
            if normalized is None:
                self.issue(op, f"{path}.{key}", resource)
                continue
            safe[key] = normalized
            pointer = f"/{field}" if field else f"/attributes/{key}"
            facts[pointer] = (f"{path}.{key}", normalized)
            if field:
                fields[field] = normalized
            else:
                resource.attributes[key] = normalized
            if key == "RedrivePolicy":
                for target_field, source_field in (
                    ("dead_letter_target_arn", "deadLetterTargetArn"),
                    ("max_receive_count", "maxReceiveCount"),
                ):
                    fields[target_field] = normalized[source_field]
                    facts[f"/{target_field}"] = (
                        f"{path}.{key}.{source_field}",
                        normalized[source_field],
                    )
        if not isinstance(resource, SqsQueue):
            resource.attributes.update(fields)
            facts = {
                f"/attributes{pointer}" if not pointer.startswith("/attributes/") else pointer: fact
                for pointer, fact in facts.items()
            }
        # Only safe, decoded, allowlisted fields may enter whole-response evidence.
        whole_id = self.whole(resource, op, response.path, {"Attributes": safe})
        for pointer, (source_path, value) in facts.items():
            self.cite(resource, pointer, op, source_path, value)
        if isinstance(resource, SqsQueue):
            for field, value in fields.items():
                setattr(resource, field, value)

        # Preserve policy-specific redaction paths, including condition context and tag values.
        if "Policy" in safe:
            original_safe, paths = sanitize_document(item["Policy"])

            def tag_paths(before, after, pointer=""):
                if isinstance(before, dict) and isinstance(after, dict):
                    for key in before.keys() & after.keys():
                        yield from tag_paths(
                            before[key],
                            after[key],
                            f"{pointer}/{escape_pointer_token(key)}",
                        )
                elif isinstance(before, list) and isinstance(after, list):
                    for index, (left, right) in enumerate(zip(before, after)):
                        yield from tag_paths(left, right, f"{pointer}/{index}")
                elif before != after:
                    yield pointer

            paths += list(tag_paths(original_safe, safe["Policy"]))
            if paths:
                pointer = "/policy" if isinstance(resource, SqsQueue) else "/attributes/policy"
                self.redactions(resource.fact_evidence[pointer], paths)
                self.redactions([whole_id], paths, "/Attributes/Policy")

    def collect(self):
        pages = self.run(
            Op.LIST_QUEUES,
            [
                DetailCall(
                    Op.LIST_QUEUES,
                    {"MaxResults": 1000},
                    paginate=True,
                )
            ],
        )
        urls = {}
        for response in pages:
            page = response.value
            values = page.get("QueueUrls", []) if isinstance(page, dict) else None
            if not isinstance(values, list):
                self.issue(Op.LIST_QUEUES, f"{response.path}.QueueUrls")
                continue
            for index, value in enumerate(values):
                path = f"{response.path}.QueueUrls[{index}]"
                if not queue_url(value):
                    self.issue(Op.LIST_QUEUES, path)
                    continue
                urls.setdefault(value, path)
        # Inventory counts are queues, not HTTP pages.
        coverage = self.coverage[Op.LIST_QUEUES]
        if coverage.status in {CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE}:
            self.coverage[Op.LIST_QUEUES] = coverage.model_copy(update={"result_count": len(urls)})
        attributes = self.run(
            Op.GET_QUEUE_ATTRIBUTES,
            [
                DetailCall(Op.GET_QUEUE_ATTRIBUTES, {"QueueUrl": url, "AttributeNames": ["All"]})
                for url in urls
            ],
            Op.LIST_QUEUES,
        )
        by_url = {response.call.params["QueueUrl"]: response for response in attributes}
        resources = {}
        for url, source_path in urls.items():
            response = by_url.get(url)
            item = (
                response.value.get("Attributes")
                if response and isinstance(response.value, dict)
                else None
            )
            arn = item.get("QueueArn") if isinstance(item, dict) else None
            trusted = queue_arn(arn)
            identity = ResourceIdentity(
                account_id=self.account_id,
                region=self.region,
                service=AwsService.SQS,
                physical_id=arn if trusted else url,
            )
            resource: Resource
            if trusted:
                resource = SqsQueue(identity=identity, arn=arn, queue_url=url)
            else:
                resource = UnknownResource(
                    identity=identity,
                    aws_resource_type=ResourceType.SQS_QUEUE,
                    attributes={"queue_url": url},
                )
            self.whole(resource, Op.LIST_QUEUES, source_path, url)
            self.cite(
                resource,
                "/queue_url" if trusted else "/attributes/queue_url",
                Op.LIST_QUEUES,
                source_path,
                url,
            )
            if not trusted:
                request = json.dumps(
                    {"QueueUrl": url, "AttributeNames": ["All"]},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                path = (
                    f"{response.path}.Attributes.QueueArn"
                    if response
                    else f"Request[{request}].Attributes.QueueArn"
                )
                self.issue(Op.GET_QUEUE_ATTRIBUTES, path, resource)
            if response:
                self.attributes(resource, response)
            resources[url] = resource
            self.resources.append(resource)
        tags = self.run(
            Op.LIST_QUEUE_TAGS,
            [DetailCall(Op.LIST_QUEUE_TAGS, {"QueueUrl": url}) for url in urls],
            Op.LIST_QUEUES,
        )
        for response in tags:
            resource = resources[response.call.params["QueueUrl"]]
            value = response.value.get("Tags", {}) if isinstance(response.value, dict) else None
            if not isinstance(value, dict) or not all(
                isinstance(key, str) and isinstance(child, str) for key, child in value.items()
            ):
                self.issue(Op.LIST_QUEUE_TAGS, f"{response.path}.Tags", resource)
                continue
            # Tag values are untrusted free text even when their key looks innocuous.
            safe = {key: "[REDACTED]" for key in value}
            resource.attributes["tags"] = safe
            whole_id = self.whole(resource, Op.LIST_QUEUE_TAGS, response.path, {"Tags": safe})
            self.cite(
                resource, "/attributes/tags", Op.LIST_QUEUE_TAGS, f"{response.path}.Tags", safe
            )
            paths = [f"/{escape_pointer_token(key)}" for key in safe]
            self.redactions([whole_id], paths, "/Tags")
            self.redactions(resource.fact_evidence["/attributes/tags"], paths)
        return ServiceCollection(self.resources, self.evidence, list(self.coverage.values()))


def collect_sqs(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
) -> ServiceCollection:
    return _Collector(session, account, context, snapshot_id, account_id, region).collect()
