# CloudFormation template import

The offline `CloudFormationRequest` / `CloudFormationAdapter` accepts JSON and YAML
templates through `collect_snapshots` and `collect_snapshots_async`. Output is a
complete schema `2` snapshot with `source.kind="cloudformation"`: declarations,
not deployed observations. Existing schema `1` files remain readable.

```python
from archdiscov.ingestion.cloudformation import CloudFormationRequest
from archdiscov.services import collect_snapshots, get_evidence, save_snapshot

request = CloudFormationRequest(
    path="tests/fixtures/cloudformation/template.yaml",
    account_id="123456789012",
    region="eu-west-1",
    stack_name="demo",
    parameters={"Stage": "test"},
)
snapshot = collect_snapshots(request)[0]
save_snapshot(snapshot, "snapshots/cloudformation-example.json")
print(get_evidence(snapshot, next(iter(snapshot.evidence))))
```

Run examples with `.venv/bin/python` from the checkout after locked installation.
Choose a new destination in an existing directory: persistence never overwrites.
The [raw-source CLI](raw-source-ingestion.md) exposes this adapter through
`ingest --format cloudformation`, with explicit account/region and optional
stack, parameter-file, and sensitive-path context.

## Context and identity

- Account and concrete region are required; the importer never guesses deployment
  context from template text or contacts AWS. IAM declarations use `global` scope.
- Optional stack name/ARN scopes logical addresses as `stack-context/LogicalId`.
  Without stack context the exact logical ID is retained. Supply stack context to
  distinguish templates belonging to different stacks. A supplied stack ARN must
  agree with account, region, and any supplied name.
- Physical IDs and ARNs are never invented from logical IDs or configured names.
  ARN-requiring typed models therefore remain `UnknownResource` summaries when
  importing declarations.
- Pseudo parameters use supplied account, region, partition, and optional stack
  context. `AWS::URLSuffix` is available for commercial, China, and GovCloud scopes.
  Missing context remains unresolved. There is no stack lookup.

## Safe parsing and bounded expressions

PyYAML uses a restricted safe loader with explicit CloudFormation tag constructors;
JSON long forms and YAML short forms have equivalent behavior. Duplicate keys,
arbitrary object tags, recursive aliases, non-JSON values, malformed templates,
and excessive input structure fail with safe diagnostics. Only declarations under
`Resources` are imported; outputs and arbitrary metadata are not copied.
Templates support the optional format version `2010-09-09`, including its unquoted
YAML spelling. Input is limited to 1 MiB, depth 64, and 50,000 expanded JSON nodes;
evaluation also bounds traversal and generated values.

The local evaluator supports literal values, parameter/pseudo `Ref`, local-resource
`Ref`/`GetAtt` references, `Sub`, `Join`, `Split`, `Select`, `FindInMap`, `If`, named
conditions, and `Equals`/`And`/`Or`/`Not`. Evaluation is bounded and never runs template
code. Parameter `Number` and list forms are prepared locally; SSM parameter-value
types require remote resolution and remain unavailable. Supplied undeclared or
wrong-type parameters fail safely; missing parameters remain unresolved.
This is not a full deployment validator: parameter constraints and template Rules
are not evaluated.

Local resource references create links to stored declarations, but their eventual
physical values remain unavailable: references are not replaced by fabricated IDs.
Unknown descendants are omitted; lists with unavailable entries are conservatively
omitted rather than shifted into misleading fact citations. Explicit null remains
distinct from an unavailable value. False resource conditions omit that declaration;
unresolved conditions omit it with coverage limitations.

`ImportValue`, `GetAZs`, unsupported intrinsics, transforms/macros, and unavailable
expressions remain limitations, not evaluated AWS configuration. External imports
and missing declaration references have explicit unresolved targets. Nested stacks
remain resources with unavailable contents; no `TemplateURL`, nested parameters, or
external template contents are fetched or persisted. No deployment, Terraform,
provider, network, or arbitrary code execution is performed.

## Facts and relationships

| Declaration | Retained facts | Source locations under `Properties` |
| --- | --- | --- |
| VPC/subnet | Valid CIDRs; subnet availability zone | `CidrBlock`, `Ipv6CidrBlock`, `AvailabilityZone` |
| EC2 instance | Instance type | `InstanceType` |
| Core EC2/network | Literal VPC/subnet/group identifiers; safe Name tag | `VpcId`, `SubnetId`, `SecurityGroupIds`, `GroupSet`, `Tags[i].Value` |
| NAT and egress-only gateways | `ConnectivityType` → `connectivity`; literal subnet and VPC identifiers as typed fields | `ConnectivityType`, `SubnetId`, `VpcId` |
| VPC peering connections | Literal `VpcId` and `PeerVpcId` as each side's VPC, `PeerOwnerId` as the accepter's owner, `PeerRegion` as an attribute; a `!Ref` side stays unset and is a reference | `VpcId`, `PeerVpcId`, `PeerOwnerId`, `PeerRegion` |
| VPC endpoints | `VpcEndpointType` → `endpoint_type`; `ServiceName`; literal `RouteTableIds`, `SubnetIds`, and `SecurityGroupIds` items, each cited; a `!Ref` item is a reference | `VpcEndpointType`, `ServiceName`, `VpcId`, `RouteTableIds`, `SubnetIds`, `SecurityGroupIds` |
| Managed prefix lists | `PrefixListName` → name; `AddressFamily`; each literal `Entries[i].Cidr`, recorded whole or not at all (descriptions are not kept) | `PrefixListName`, `AddressFamily`, `Entries[i].Cidr` |
| Lambda summary | Name, runtime, role reference, memory, timeout | `FunctionName`, `Runtime`, `Role`, `MemorySize`, `Timeout` |
| SQS summary | Name, visibility timeout, retention | `QueueName`, `VisibilityTimeout`, `MessageRetentionPeriod` |
| RDS summary | Identifier, engine/version, class/port as applicable | Selected scalar configuration only |
| ECS, IAM, ELBv2 summaries | Selected names/path/protocol/port/scheme/type | Selected scalar configuration only |
| Nested stacks/unsupported types | AWS type and safe Name tag only | `Type`, `Tags[i].Value` |

Typed core models cover VPCs, subnets, instances, security groups, internet gateways, route tables,
ENIs, NAT gateways, egress-only internet gateways, VPC peering connections, VPC endpoints, and
managed prefix lists. A NAT gateway's address comes from a referenced `AWS::EC2::EIP`, which stays
an unsupported summary, so `public_ips` stays empty. A declared value a typed model rejects (such as
an unknown `ConnectivityType`) leaves an unsupported summary with its safe Name tag and a coverage
limitation. Rules, routes, policy documents, workload definitions, authentication, environment
values, and other complex configurations are not mapped. Unmapped properties and unsupported
summaries qualify coverage rather than implying complete configuration.

Containment follows direct local VPC/subnet references of the expected type, without
inventing missing parents. All resources are stored once. `Ref`, `GetAtt`, and
`DependsOn` produce source-owned `attached_to` dependencies with
`AttachmentKind.CLOUDFORMATION_REFERENCE`; these describe configuration references,
not deployed attachment, successful access, effective permissions, or reachability.
Missing/conditional targets remain unresolved, and no referenced account is traversed.

## Evidence, sensitivity, and coverage

Evidence locates the input file and exact JSON Pointer within the template; YAML
uses the equivalent parsed structure rather than line/column provenance. Fact pointers
refer to retained normalized values. Evaluated literals cite their source expression;
unavailable facts have unresolved evidence and coverage, not dangling fact pointers.
No raw whole-template, opaque property, parameter map, or unevaluated expression is
stored as evidence.

`NoEcho`, generic sensitive-key scrubbing, secret-named tag pairs, and explicitly
supplied `sensitive_paths` run before persistence. Sensitive paths are template-relative
JSON Pointers, for example `/Resources/Server/Properties/UserData` or
`/Parameters/Opaque/Default`. NoEcho and protected values propagate through expressions
without exposing the secret; redaction records retain safe locations, not raw values.
Arbitrary secrets in otherwise allowed literal names cannot be inferred reliably:
use NoEcho or explicit sensitive paths for protected inputs. Do not put secrets in
logical IDs, file paths, stack context, or other identifiers.
Sensitive paths can target resource properties, parameter defaults, mapping values,
and conditions, not structural identities such as resource Type or logical IDs.

One `cloudformation.template` operation is emitted per region/service scope with
retained resource count. Unknown values, redactions, unsupported mappings, and omitted
conditional declarations produce `incomplete` coverage with exact locations. Complete
empty input remains successful zero. Persistence revalidates integrity and sanitization;
evidence inspection uses the same public API as AWS and Terraform imports.