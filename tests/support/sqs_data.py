"""An SQS queue's attributes and a fake SQS client that serves them."""

import json
from copy import deepcopy
from threading import Lock

ACCOUNT = "123456789012"
URL = f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/work"
ARN = f"arn:aws:sqs:us-east-1:{ACCOUNT}:work"
DLQ = f"arn:aws:sqs:us-east-1:{ACCOUNT}:dead"


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
