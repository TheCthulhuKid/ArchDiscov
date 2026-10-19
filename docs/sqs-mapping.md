# SQS configuration mapping

`collect_sqs(session, account, context, snapshot_id, account_id, region)` returns a
flat `ServiceCollection(resources, evidence, coverage)` for one account and region.
The collector reads configuration only. It never collects messages, message bodies,
message attributes, receipt handles, or workload counters.

## Exact permissions and calls

| IAM permission | API request | Scope |
| --- | --- | --- |
| `sqs:ListQueues` | `ListQueues(MaxResults=1000, NextToken=...)` | Regional queue URL inventory; permission resource `*` |
| `sqs:GetQueueAttributes` | `GetQueueAttributes(QueueUrl=..., AttributeNames=['All'])` | Each observed queue; queue ARN permission scope |
| `sqs:ListQueueTags` | `ListQueueTags(QueueUrl=...)` | Each observed queue; queue ARN permission scope |

No `ReceiveMessage`, `SendMessage`, `PurgeQueue`, deletion, mutation, or additional
SQS operation is allowed. An action named inside an observed policy is configuration
data, not an API call or required collector permission. No KMS API read or decrypt
permission is required. Credentials and STS account verification are shared adapter
concerns, not additional SQS reads.

`MaxResults=1000` enables AWS paging. Every returned `NextToken` is sent as the next
request's `NextToken`; repeated tokens and page errors qualify retained inventory.
Detail calls use the account's bounded concurrency, timeouts, and retry settings.

## Identity and fields

Only an observed, valid `Attributes.QueueArn` establishes a typed `SqsQueue` and its
ARN physical identity. `QueueUrls[index]` is separately recorded as `queue_url`.
An unavailable/malformed/missing ARN retains an `UnknownResource` with
`aws_resource_type='AWS::SQS::Queue'`, the observed URL as its physical identity,
`attributes.queue_url`, and an explicit limitation. No ARN is synthesized from a URL,
queue name, account, or region. Available safe configuration survives under attributes
on these unknown resources, with citations addressing those actual attribute fields.

There are 14 SQS-specific typed fields, in addition to the resource discriminator and
inherited resource fields:

| `SqsQueue` field | Source |
| --- | --- |
| `arn` | `Attributes.QueueArn` |
| `queue_url` | `ListQueues.QueueUrls[index]` |
| `policy` | Decoded `Attributes.Policy` object |
| `retention_seconds` | `Attributes.MessageRetentionPeriod` |
| `visibility_timeout_seconds` | `Attributes.VisibilityTimeout` |
| `delay_seconds` | `Attributes.DelaySeconds` |
| `fifo_queue` | `Attributes.FifoQueue` |
| `content_based_deduplication` | `Attributes.ContentBasedDeduplication` |
| `kms_master_key_id` | `Attributes.KmsMasterKeyId` |
| `kms_data_key_reuse_period_seconds` | `Attributes.KmsDataKeyReusePeriodSeconds` |
| `sqs_managed_sse_enabled` | `Attributes.SqsManagedSseEnabled` |
| `dead_letter_target_arn` | Decoded `Attributes.RedrivePolicy.deadLetterTargetArn` |
| `max_receive_count` | Decoded `Attributes.RedrivePolicy.maxReceiveCount` |
| `redrive_allow_policy` | Decoded, allowlisted `Attributes.RedriveAllowPolicy` |

Safe generic attributes additionally retain `MaximumMessageSize`,
`ReceiveMessageWaitTimeSeconds`, `CreatedTimestamp`, `LastModifiedTimestamp`,
`DeduplicationScope`, `FifoThroughputLimit`, decoded `RedrivePolicy`, and redacted
`tags` when available. Unknown attribute keys and approximate message counters are
excluded even from whole-response evidence. Integer and boolean parsing preserves
zero and false; invalid types, ranges, JSON, and references are unavailable facts,
not defaults. Optional attributes absent from a successful response remain unknown;
absence does not infer disabled encryption, absence of redrive, or effective access.

## Evidence, safety, and limitations

Evidence locations include canonical request parameters, page indexes, and the exact
source field. For example, the `/dead_letter_target_arn` citation ends in
`.Attributes.RedrivePolicy.deadLetterTargetArn`, not merely the whole queue response.
`/policy` cites `.Attributes.Policy`; stored policy values are decoded JSON objects.
Redrive paths describe fields inside the decoded JSON configuration.

Before whole-response evidence is created, policy/redrive JSON is decoded and
allowlisted. Malformed raw strings are removed entirely and replaced by omitted
evidence with a constant limitation and an aggregate unresolved configuration path.
API exception text is not retained. IAM policy normalization/redaction is reused for
secrets and sensitive condition context such as `sts:ExternalId`; generic resource,
principal, and request tag condition values and all queue tag values are also redacted. Typed
policy construction and serialization sanitize documents, including in-place policy
mutation before persistence. Redaction paths identify removed values.

Redrive policy retains only `deadLetterTargetArn` and validated `maxReceiveCount`.
Redrive allow policy retains only `redrivePermission` and validated `sourceQueueArns`.
Neither resource policies nor redrive allow configuration imply effective permissions,
reachability, successful delivery, or actual message movement. The shared relationship
resolver owns dead-letter target resolution from the exact cited
`dead_letter_target_arn` field; this collector does not invent target resources.

Queue policies also produce principal references. During account assembly, each principal
entry of each stored policy statement becomes one `resource_policy_principal` dependency,
using the same principal rules as IAM role trust policies: collected role ARNs resolve
locally; account, ARN, service, and federated principals are external targets; wildcard,
`NotPrincipal`, and unsupported forms stay unresolved. Each edge cites the principal entry and
its whole sanitized statement, so `Effect` and `Condition` appear only in that statement
evidence. This uses the stored policy without new API calls, and records configured policy,
not effective access. Terraform and CloudFormation imports do not ingest queue policies, so
they produce no such edges.

Coverage has exactly one record for each of the three operations. Empty successful
inventory gives three successful zero counts without detail requests. Discovery
failure skips dependent reads; partial discovery qualifies detail coverage even when
all discovered queues were read. Detail/page failures retain successful siblings and
aggregate safe failure records. Missing ARN details do not erase observed URLs, and
malformed fields do not abort other valid fields or queues.