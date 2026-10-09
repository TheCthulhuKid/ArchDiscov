### Lambda mapping and permissions

`collect_lambda(session, account, context, snapshot_id, account_id, region)` returns a
`ServiceCollection` with flat `LambdaFunction` resources, evidence, and exactly one
aggregate coverage record for each of these operations:

| API / permission | Scope |
| --- | --- |
| `ListFunctions` / `lambda:ListFunctions` | Regional discovery; all pages via request `Marker`, response `NextMarker`. |
| `GetFunctionConfiguration` / `lambda:GetFunctionConfiguration` | One read per distinct discovered function ARN, bounded by account concurrency. |

List permissions require resource `*`; configuration permission can be scoped to the
function ARNs being inventoried. This collector uses the caller's existing session;
identity/assume-role permissions belong to the parent adapter. No code is fetched:
`GetFunction`, code URLs, versions/aliases, policies, tags, invocation/event-source
mappings, function URLs, concurrency, and code-signing APIs are outside this slice.

### Resource fields and citations

The resource type is `AWS::Lambda::Function`. Identity uses the function ARN and
`lambda` service, not a display name. Both APIs share this source mapping:

| Model field | AWS source field |
| --- | --- |
| `arn`, `name` | `FunctionArn`, `FunctionName` |
| `role_arn` | `Role` |
| `vpc_id` | `VpcConfig.VpcId` |
| `subnet_ids`, `security_group_ids` | `VpcConfig.SubnetIds`, `VpcConfig.SecurityGroupIds` |
| `runtime`, `package_type` | `Runtime`, `PackageType` |
| `state`, `last_update_status` | `State`, `LastUpdateStatus` |
| `memory_size`, `timeout` | `MemorySize`, `Timeout` |
| `architectures` | `Architectures` |
| `configuration` | Allowlisted projection of the latest successful source item. |

Every observed typed fact has an exact request/page/field citation, with additional
per-element citations for lists. For example, `/subnet_ids/0` cites
`Request[{"FunctionName":"<arn>"}].Page[0].VpcConfig.SubnetIds[0]` from
`lambda:GetFunctionConfiguration`. Inventory facts cite
`Request[{}].Page[n].Functions[i].<field>`. Whole-item evidence is already projected
before it is constructed. The parent resolver consumes `role_arn`, `subnet_ids`, and
`security_group_ids`; this collector neither resolves references nor creates edges.

### Safety and uncertainty

The source projection retains only validated identity and mapped configuration fields.
It excludes **all environment variable values regardless of variable name** (the
entire `Environment` object is omitted), description text, code/signing locations,
raw runtime/state/update diagnostics, SDK metadata, authentication tokens, and unknown
fields. Known excluded containers have redaction paths/status in whole-item evidence;
unknown fields are omitted rather than copied. No free-text reason or error message is
used as a normalized fact. Model validators repeat configuration projection on direct
construction and snapshot save, including configuration mutated after construction.

Configuration enums, runtimes, identifiers, number bounds, and list shapes are
validated without coercion. Unsupported/malformed mapped facts become unavailable
with precise `unresolved_values` source paths, not raw input text. Fields absent from
an optional API response remain unavailable; omitted out-of-scope fields are not
claims of completeness. A legitimate empty subnet/security-group list differs from
`None`; AWS's empty `VpcId` is retained as explicit non-VPC configuration.

Successful details supersede the same observed inventory fields. Failed details retain
safe inventory and its citations without inventing empty configuration collections.
Partial/malformed discovery qualifies downstream detail coverage as incomplete, or
skipped when no usable functions were discovered. Successful empty discovery has zero
results for both operations without issuing detail requests. Missing permissions,
throttling, pagination failures, and concurrent resource changes can limit coverage;
the result is not proof of absent dependencies or effective runtime behavior.