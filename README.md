# Archdiscov

Archdiscov answers questions about an AWS account with proof. Every claim cites the AWS API
response it came from, and "unknown" is a valid answer when the evidence is missing.

**Status:** v0.1 in progress. The `archdiscov` command is not available yet.

## Demo snapshot: Acme

`demo/acme/` is a snapshot of a fictional company's AWS account, used by the demo and the tests.
A snapshot is a folder of unmodified AWS API responses, exactly what the AWS CLI prints with
`--output json`, plus a `manifest.json` that says which operations were recorded.

```text
demo/acme/
  manifest.json                       account, region, capture time, recorded operations
  ec2/describe-instances.json
  ec2/describe-subnets.json
  ec2/describe-route-tables.json
  ec2/describe-internet-gateways.json
  ec2/describe-network-acls.json
  ec2/describe-security-groups.json
  rds/describe-db-instances.json
```

The account is `111122223333` in `eu-west-2`, with one VPC (`10.0.0.0/16`):

- `subnet-public-a` and `subnet-public-b` route `0.0.0.0/0` to the internet gateway `igw-acme`.
- `subnet-private-a` and `subnet-private-b` have a local route only.
- `subnet-public-b` has its own network ACL, `nacl-public-b`, which denies inbound tcp 3389.

| Resource | Setup | Expected |
| --- | --- | --- |
| `i-web` | public IP, 443 open to the world | exposed tcp 443 |
| `i-bastion` | public IP, 22 open to the world | exposed tcp 22 |
| `i-legacy` | public IP, 3389 open to the world, in `subnet-public-b` | not exposed: the network ACL denies 3389 |
| `i-worker` | private subnet, no public IP | not exposed |
| `db-orders` | publicly accessible, both public subnets, 5432 open to the world | exposed tcp 5432 |
| `db-analytics` | publicly accessible, one public and one private subnet | unknown: AWS doesn't record which subnet hosts it |
| `db-billing` | not publicly accessible, 5432 open to the world | not exposed |

The responses were written by hand, not captured from a real account. Resource IDs are
readable stand-ins (`i-web` instead of `i-0a1b2c...`), and public IPs come from
`198.51.100.0/24`, a range reserved for documentation.
