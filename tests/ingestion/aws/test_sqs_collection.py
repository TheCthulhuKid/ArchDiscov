import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.queues import collect_sqs
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.queues import SqsQueue
from tests.support.sqs_data import ACCOUNT, ARN, DLQ, URL, Client, attributes

REGION = AwsRegion.US_EAST_1
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SNAPSHOT = UUID(int=16)
CONTEXT = AdapterContext(clock=lambda: NOW)
CONFIG = AwsAccountConfig(
    profile="offline",
    expected_account_id=ACCOUNT,
    regions=(REGION,),
    services=(AwsService.SQS,),
    max_concurrency=2,
)


def session(client):
    def get_client(name, **kwargs):
        assert name == "sqs"
        assert kwargs["region_name"] == REGION
        return client

    return SimpleNamespace(client=get_client)


def collect(client):
    return collect_sqs(session(client), CONFIG, CONTEXT, SNAPSHOT, ACCOUNT, REGION)


def dump(result):
    return json.dumps(
        {
            "resources": [resource.model_dump(mode="json") for resource in result.resources],
            "evidence": {
                key: evidence.model_dump(mode="json") for key, evidence in result.evidence.items()
            },
            "coverage": [coverage.model_dump(mode="json") for coverage in result.coverage],
        }
    )


def coverage(result, operation):
    return next(item for item in result.coverage if item.operation == operation)


def denied(operation):
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-exception"}}, operation
    )


def test_configuration_safe_evidence_and_exact_citations():
    client = Client()
    result = collect(client)
    (queue,) = result.resources
    assert isinstance(queue, SqsQueue)
    assert queue.arn == queue.identity.physical_id == ARN
    assert queue.queue_url == URL
    assert queue.retention_seconds == 345600
    assert queue.visibility_timeout_seconds == queue.delay_seconds == 0
    assert queue.fifo_queue is queue.content_based_deduplication is False
    assert queue.sqs_managed_sse_enabled is True
    assert queue.dead_letter_target_arn == DLQ
    assert queue.max_receive_count == 4
    assert queue.redrive_allow_policy == {"redrivePermission": "byQueue", "sourceQueueArns": [DLQ]}
    assert queue.policy["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    assert {item.status for item in result.coverage} == {"success"}
    assert len(result.coverage) == 3
    assert [item.result_count for item in result.coverage] == [1, 1, 1]
    assert client.calls == [
        ("list_queues", {"MaxResults": 1000}),
        ("get_queue_attributes", {"QueueUrl": URL, "AttributeNames": ["All"]}),
        ("list_queue_tags", {"QueueUrl": URL}),
    ]
    for pointer, suffix in (
        ("/policy", ".Attributes.Policy"),
        ("/dead_letter_target_arn", ".Attributes.RedrivePolicy.deadLetterTargetArn"),
        ("/max_receive_count", ".Attributes.RedrivePolicy.maxReceiveCount"),
    ):
        (evidence_id,) = queue.fact_evidence[pointer]
        assert result.evidence[evidence_id].configuration_path.endswith(suffix)
    policy_evidence = result.evidence[queue.fact_evidence["/policy"][0]]
    assert "/Statement/0/Condition/StringEquals/sts:ExternalId" in policy_evidence.redacted_paths
    text = dump(result)
    assert "secret-" not in text
    assert "ApproximateNumberOfMessages" not in text
    assert "MessageBody" not in text


@pytest.mark.parametrize(
    "listed,status",
    [(denied("ListQueues"), "failed"), ({"QueueUrls": "secret-not-a-list"}, "incomplete")],
    ids=["failed", "malformed"],
)
def test_unusable_discovery_skips_detail_calls_without_raw_values(listed, status):
    client = Client({"list_queues": listed})
    result = collect(client)
    assert result.resources == []
    assert [item.status for item in result.coverage] == [status, "skipped", "skipped"]
    assert "secret-" not in dump(result)
    assert len(client.calls) == 1


def test_queue_without_a_trusted_arn_is_an_unknown_resource_named_by_url():
    result = collect(Client({"get_queue_attributes": denied("GetQueueAttributes")}))
    (queue,) = result.resources
    assert isinstance(queue, UnknownResource)
    assert queue.identity.physical_id == URL
    assert queue.attributes["queue_url"] == URL
    assert queue.attributes["limitations"]
    assert "arn:aws:sqs" not in dump(result)
    assert coverage(result, "GetQueueAttributes").status == "failed"
    assert coverage(result, "ListQueueTags").status == "success"
    assert "secret-" not in dump(result)

    attrs = attributes()
    del attrs["QueueArn"]
    result = collect(Client({"get_queue_attributes": {"Attributes": attrs}}))
    (queue,) = result.resources
    assert isinstance(queue, UnknownResource)
    assert queue.identity.physical_id == URL
    representation = queue.model_dump(mode="json")
    # Every citation must address a field that actually exists on the unknown resource.
    for pointer in queue.fact_evidence:
        value = representation
        for component in pointer.split("/")[1:]:
            value = value[component.replace("~1", "/").replace("~0", "~")]
    assert coverage(result, "GetQueueAttributes").status == "incomplete"


def test_malformed_field_is_unavailable_not_raw_and_keeps_its_siblings():
    attrs = attributes()
    attrs["Policy"] = "{secret-malformed-policy"
    result = collect(Client({"get_queue_attributes": {"Attributes": attrs}}))
    (queue,) = result.resources
    assert isinstance(queue, SqsQueue)
    assert queue.policy is None and queue.retention_seconds == 345600
    assert coverage(result, "GetQueueAttributes").status == "incomplete"
    unavailable = [item for item in result.evidence.values() if item.value_status == "omitted"]
    assert any(item.configuration_path.endswith(".Attributes.Policy") for item in unavailable)
    assert "secret-" not in dump(result)


def test_typed_model_sanitizes_constructed_and_mutated_redrive_allow():
    queue = collect(Client()).resources[0]
    data = queue.model_dump()
    data["policy"] = {
        "Statement": [
            {
                "Condition": {"StringEquals": {"sts:ExternalId": "secret-direct"}},
                "Tags": [{"Key": "team", "Value": "secret-tag-direct"}],
            }
        ]
    }
    queue = SqsQueue.model_validate(data)
    assert "secret-" not in queue.model_dump_json()
    queue.redrive_allow_policy["unknown"] = "secret-mutated-extra"
    queue.redrive_allow_policy["sourceQueueArns"] = ["secret-mutated-arn"]
    assert "secret-" not in queue.model_dump_json()


def test_typed_model_requires_arn_identity():
    data = collect(Client()).resources[0].model_dump()
    data.pop("id")
    data["identity"]["physical_id"] = URL
    with pytest.raises(ValidationError, match="SQS identity must use the resource ARN"):
        SqsQueue.model_validate(data)


def test_real_stubber_accepts_the_exact_requests():
    client = boto3.client(
        "sqs",
        region_name=REGION.value,
        aws_access_key_id="offline",
        aws_secret_access_key="offline",
        aws_session_token="offline",
    )
    with Stubber(client) as stub:
        stub.add_response("list_queues", {"QueueUrls": [URL]}, {"MaxResults": 1000})
        stub.add_response(
            "get_queue_attributes",
            {"Attributes": {"QueueArn": ARN}},
            {"QueueUrl": URL, "AttributeNames": ["All"]},
        )
        stub.add_response("list_queue_tags", {}, {"QueueUrl": URL})
        result = collect(client)
        stub.assert_no_pending_responses()
    assert result.resources[0].arn == ARN
    assert all(item.status == "success" for item in result.coverage)
    assert [item.result_count for item in result.coverage] == [1, 1, 1]
