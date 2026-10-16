import json
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from types import SimpleNamespace
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.queues import collect_sqs
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.queues import SqsQueue

ACCOUNT = "123456789012"
REGION = AwsRegion.US_EAST_1
URL = f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/work"
ARN = f"arn:aws:sqs:us-east-1:{ACCOUNT}:work"
DLQ = f"arn:aws:sqs:us-east-1:{ACCOUNT}:dead"
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


def attributes():
    return {
        "QueueArn": ARN,
        "MessageRetentionPeriod": "345600",
        "VisibilityTimeout": "0",
        "DelaySeconds": "0",
        "FifoQueue": "false",
        "ContentBasedDeduplication": "false",
        "SqsManagedSseEnabled": "true",
        "KmsMasterKeyId": "alias/queues",
        "KmsDataKeyReusePeriodSeconds": "300",
        "MaximumMessageSize": "1048576",
        "ReceiveMessageWaitTimeSeconds": "0",
        "Policy": json.dumps(
            {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "sns.amazonaws.com"},
                        "Action": "sqs:SendMessage",
                        "Resource": ARN,
                        "Condition": {
                            "StringEquals": {
                                "sts:ExternalId": "secret-context",
                                "aws:ResourceTag/team": "secret-generic-tag-condition",
                                "aws:SourceAccount": ACCOUNT,
                            }
                        },
                    }
                ]
            }
        ),
        "RedrivePolicy": json.dumps(
            {
                "deadLetterTargetArn": DLQ,
                "maxReceiveCount": "4",
                "extra": "secret-redrive",
            }
        ),
        "RedriveAllowPolicy": json.dumps(
            {
                "redrivePermission": "byQueue",
                "sourceQueueArns": [DLQ],
                "extra": "secret-redrive-allow",
            }
        ),
        "ApproximateNumberOfMessages": "secret-counter",
        "MessageBody": "secret-message",
        "Credentials": "secret-credentials",
    }


class Client:
    def __init__(self, data=None):
        self.data = {
            "list_queues": {"QueueUrls": [URL]},
            "get_queue_attributes": {"Attributes": attributes()},
            "list_queue_tags": {"Tags": {"team": "secret-tag", "password": "secret-password"}},
        }
        self.data.update(data or {})
        self.calls = []
        self.lock = Lock()

    def __getattr__(self, name):
        def call(**params):
            with self.lock:
                self.calls.append((name, params))
            value = self.data[name]
            value = value(params) if callable(value) else value
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        return call


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


def test_configuration_safe_evidence_and_exact_citations(tmp_path):
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
    path = tmp_path / "sqs.json"
    path.write_text(dump(result))
    assert b"secret-" not in path.read_bytes()
    assert "ApproximateNumberOfMessages" not in path.read_text()
    assert "MessageBody" not in path.read_text()


@pytest.mark.parametrize("empty", [{}, {"QueueUrls": []}])
def test_empty_inventory_has_three_successful_zero_counts(empty):
    client = Client({"list_queues": empty})
    result = collect(client)
    assert result.resources == []
    assert len(result.coverage) == 3
    assert all(item.status == "success" and item.result_count == 0 for item in result.coverage)
    assert client.calls == [("list_queues", {"MaxResults": 1000})]


def test_failed_discovery_skips_detail_calls_and_sanitizes_exception():
    client = Client({"list_queues": denied("ListQueues")})
    result = collect(client)
    assert [item.status for item in result.coverage] == ["failed", "skipped", "skipped"]
    assert "secret-exception" not in dump(result)
    assert len(client.calls) == 1


def test_attributes_failure_retains_only_url_identity_with_limitation():
    result = collect(Client({"get_queue_attributes": denied("GetQueueAttributes")}))
    (queue,) = result.resources
    assert isinstance(queue, UnknownResource)
    assert queue.identity.physical_id == URL
    assert queue.attributes["queue_url"] == URL
    assert queue.attributes["limitations"]
    assert "arn:aws:sqs" not in dump(result)
    assert coverage(result, "GetQueueAttributes").status == "failed"
    assert coverage(result, "ListQueueTags").status == "success"
    assert "secret-exception" not in dump(result)


def test_partial_pagination_preserves_valid_queue_and_qualifies_downstream():
    def pages(params):
        if "NextToken" in params:
            assert params == {"MaxResults": 1000, "NextToken": "cursor"}
            return denied("ListQueues")
        return {"QueueUrls": [URL], "NextToken": "cursor"}

    result = collect(Client({"list_queues": pages}))
    assert len(result.resources) == 1
    assert all(item.status == "incomplete" for item in result.coverage)
    assert all(item.result_count == 1 for item in result.coverage)
    assert coverage(result, "GetQueueAttributes").reason


@pytest.mark.parametrize(
    "key,value",
    [
        ("Policy", "{secret-malformed-policy"),
        ("Policy", '{"Statement":"secret-wrong-statement"}'),
        ("RedrivePolicy", "{secret-malformed-redrive"),
        ("RedrivePolicy", '{"deadLetterTargetArn": "secret-invalid-arn", "maxReceiveCount": 4}'),
        ("RedrivePolicy", json.dumps({"deadLetterTargetArn": DLQ, "maxReceiveCount": False})),
        ("RedriveAllowPolicy", "{secret-malformed-allow"),
        ("RedriveAllowPolicy", '{"redrivePermission": []}'),
        ("RedriveAllowPolicy", '{"redrivePermission": "byQueue", "sourceQueueArns": []}'),
        ("VisibilityTimeout", "secret-invalid-number"),
        ("FifoQueue", "secret-invalid-boolean"),
        ("KmsMasterKeyId", "secret-invalid-key"),
    ],
)
def test_malformed_fields_are_unavailable_not_raw_and_do_not_abort(key, value):
    attrs = attributes()
    attrs[key] = value
    result = collect(Client({"get_queue_attributes": {"Attributes": attrs}}))
    assert isinstance(result.resources[0], SqsQueue)
    assert coverage(result, "GetQueueAttributes").status == "incomplete"
    unavailable = [item for item in result.evidence.values() if item.value_status == "omitted"]
    assert any(item.configuration_path.endswith(f".Attributes.{key}") for item in unavailable)
    assert "secret-" not in dump(result)


def test_missing_arn_does_not_invent_identity_and_keeps_safe_configuration():
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


def test_mutable_typed_policy_is_sanitized_at_serialization():
    queue = collect(Client()).resources[0]
    queue.policy["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"] = "secret-later"
    queue.policy["Statement"][0]["Condition"]["StringEquals"]["aws:RequestTag/team"] = (
        "secret-tag-later"
    )
    assert "secret-" not in queue.model_dump_json()


def test_partial_detail_failures_retain_siblings_and_aggregate_once():
    second_url = URL + "-other"

    def detail(params):
        if params["QueueUrl"] == second_url:
            return denied("GetQueueAttributes")
        return {"Attributes": attributes()}

    result = collect(
        Client(
            {
                "list_queues": {"QueueUrls": [URL, second_url]},
                "get_queue_attributes": detail,
                "list_queue_tags": denied("ListQueueTags"),
            }
        )
    )
    assert len(result.resources) == 2
    assert sum(isinstance(resource, SqsQueue) for resource in result.resources) == 1
    assert sum(isinstance(resource, UnknownResource) for resource in result.resources) == 1
    attrs = coverage(result, "GetQueueAttributes")
    assert attrs.status == "incomplete" and attrs.result_count == 1
    assert len(attrs.failures) == 1
    assert coverage(result, "ListQueueTags").status == "failed"
    assert len(result.coverage) == 3
    assert "secret-" not in dump(result)


def test_repeated_pagination_token_is_incomplete_without_duplicate_details():
    result = collect(Client({"list_queues": {"QueueUrls": [URL], "NextToken": "same"}}))
    assert len(result.resources) == 1
    assert coverage(result, "ListQueues").status == "incomplete"
    assert coverage(result, "ListQueues").result_count == 1
    assert all(item.reason for item in result.coverage[1:])


@pytest.mark.parametrize("page", [{"QueueUrls": "secret-not-a-list"}, {"QueueUrls": [None]}])
def test_malformed_inventory_does_not_become_successful_empty(page):
    result = collect(Client({"list_queues": page}))
    assert result.resources == []
    assert [item.status for item in result.coverage] == ["incomplete", "skipped", "skipped"]
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


def test_mutated_malformed_policy_serializes_without_raw_value_or_warning(recwarn):
    queue = collect(Client()).resources[0]
    queue.policy = "{secret-mutated-malformed"
    assert "secret-" not in queue.model_dump_json()
    assert not recwarn


def test_model_serializer_respects_excluded_document_fields():
    queue = collect(Client()).resources[0]
    assert queue.model_dump(include={"arn"}) == {"arn": ARN}


@pytest.mark.parametrize("physical_id,service", [(URL, "sqs"), (ARN, "ec2")])
def test_typed_model_requires_arn_identity_and_sqs_service(physical_id, service):
    data = collect(Client()).resources[0].model_dump()
    data.pop("id")
    data["identity"]["physical_id"] = physical_id
    data["identity"]["service"] = service
    with pytest.raises(ValidationError, match="SQS identity must use the resource ARN"):
        SqsQueue.model_validate(data)


def test_malformed_tags_are_unavailable_not_raw():
    result = collect(Client({"list_queue_tags": {"Tags": {"team": ["secret-tag-list"]}}}))
    assert coverage(result, "ListQueueTags").status == "incomplete"
    assert "tags" not in result.resources[0].attributes
    assert "secret-" not in dump(result)


def test_real_stubber_uses_next_token_and_exact_attribute_request():
    client = boto3.client(
        "sqs",
        region_name=REGION.value,
        aws_access_key_id="offline",
        aws_secret_access_key="offline",
        aws_session_token="offline",
    )
    with Stubber(client) as stub:
        stub.add_response(
            "list_queues", {"QueueUrls": [URL], "NextToken": "cursor"}, {"MaxResults": 1000}
        )
        stub.add_response("list_queues", {}, {"MaxResults": 1000, "NextToken": "cursor"})
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


def test_real_stubber_attribute_failure_does_not_fabricate_arn():
    client = boto3.client(
        "sqs",
        region_name=REGION.value,
        aws_access_key_id="offline",
        aws_secret_access_key="offline",
        aws_session_token="offline",
    )
    with Stubber(client) as stub:
        stub.add_response("list_queues", {"QueueUrls": [URL]}, {"MaxResults": 1000})
        stub.add_client_error(
            "get_queue_attributes",
            service_error_code="AccessDenied",
            service_message="secret-aws-error",
            expected_params={"QueueUrl": URL, "AttributeNames": ["All"]},
        )
        stub.add_response(
            "list_queue_tags", {"Tags": {"team": "secret-aws-tag"}}, {"QueueUrl": URL}
        )
        result = collect(client)
        stub.assert_no_pending_responses()
    assert isinstance(result.resources[0], UnknownResource)
    assert result.resources[0].identity.physical_id == URL
    assert "secret-" not in dump(result)


@pytest.mark.parametrize(
    "operation", ["ReceiveMessage", "SendMessage", "PurgeQueue", "DeleteQueue"]
)
def test_workload_and_mutating_calls_rejected_before_client_creation(operation):
    def no_client(*args, **kwargs):
        pytest.fail("operation protection must run before client creation")

    runner = DetailRunner(
        SimpleNamespace(client=no_client),
        CONFIG,
        CONTEXT,
        service=AwsService.SQS,
        region=REGION,
        client_name="sqs",
        pagination="sqs",
    )
    with pytest.raises(IngestionError) as error:
        runner.run(operation, [DetailCall(operation, {"QueueUrl": URL})])
    assert error.value.code == "operation_not_allowed"
