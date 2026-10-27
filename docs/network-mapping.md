# EC2 network collection mapping (step 13)

`archdiscov collect` reads EC2 network configuration with six extra operations. Each resource is
stored once; attachments are `attached_to` dependencies whose `attributes["kind"]` is an
`AttachmentKind`. Nothing here concludes reachability or internet exposure.

## Permissions

`ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:DescribeInstances`, `ec2:DescribeRouteTables`,
`ec2:DescribeInternetGateways`, `ec2:DescribeNetworkAcls`, `ec2:DescribeSecurityGroups`,
`ec2:DescribeSecurityGroupRules`, `ec2:DescribeNetworkInterfaces` (plus `sts:GetCallerIdentity`,
and `sts:AssumeRole` for configured roles). All are allowlisted in
`archdiscov.ingestion.operations` and rejected otherwise before any client call. Each region and
operation has one coverage record; zero results are `success`.

## Resources and facts

| Resource | Operation | Parent | Typed facts and source path |
| --- | --- | --- | --- |
| Route table | `DescribeRouteTables` | VPC (`VpcId`) | `/routes/N` ← `RouteTables[i].Routes[n]`, with IPv4, IPv6, or prefix-list destination (`prefix_list_id` when a prefix list) |
| Internet gateway | `DescribeInternetGateways` | VPC (`Attachments[0].VpcId`) | `/attributes/attached_vpc_ids` ← `Attachments` |
| Network ACL | `DescribeNetworkAcls` | VPC (`VpcId`) | `/entries/N` ← `NetworkAcls[i].Entries[n]` |
| Security group | `DescribeSecurityGroups` + `DescribeSecurityGroupRules` | VPC (`VpcId`) | `/ingress_rules/N`, `/egress_rules/N` ← `SecurityGroupRules[k]` (or `SecurityGroups[i].IpPermissions[j]` as fallback) |
| Network interface | `DescribeNetworkInterfaces` | Subnet, else VPC | `/private_ip_addresses`, `/public_ip`, `/mac_address` ← same-named API fields; `/ipv6_addresses/N` ← `NetworkInterfaces[i].Ipv6Addresses[n]` |

VPCs and subnets (from `DescribeVpcs` and `DescribeSubnets`) add each IPv6 CIDR whose association
state is `associated` to `cidr_blocks`, after the IPv4 CIDRs, citing `/cidr_blocks/N` to
`Vpcs[i].Ipv6CidrBlockAssociationSet[n]` or `Subnets[i].Ipv6CidrBlockAssociationSet[n]`; other
states are not recorded. Security-group rules and NACL entries read IPv6 CIDRs like IPv4 ones. An
interface response without `Ipv6Addresses` leaves `ipv6_addresses` unset (not recorded); an empty
list records none.

Every resource also cites its whole API item. A parent that was referenced but not collected is
never invented: the resource is placed at the next available level (subnet → VPC → regional
root) with `attributes.unresolved_parent`.

## Relationships

| `kind` | Source → target | Source path cited |
| --- | --- | --- |
| `route_table_association` | route table → subnet (or VPC for the main association) | `RouteTables[i].Associations[n]` |
| `route_target` | route table → IGW / network interface / instance (local); NAT, egress-only internet gateway, transit gateway, peering, endpoint → external | `RouteTables[i].Routes[n]` |
| `prefix_list_reference` | route table → prefix list a route's destination names, or security group → prefix list a rule names (external) | the route or rule entry |
| `internet_gateway_attachment` | IGW → VPC | `InternetGateways[i].Attachments[n]` |
| `network_acl_association` | NACL → subnet | `NetworkAcls[i].Associations[n]` |
| `instance_interface` | network interface → instance | `NetworkInterfaces[i].Attachment` |
| `security_group_membership` | instance or interface → security group | `...SecurityGroups[n]` / `NetworkInterfaces[i].Groups[n]` |
| `security_group_reference` | security group → referenced group (local, or external when another account) | the rule entry |

Target kinds: a stored resource is `local`; a known resource outside the collection (other
account's group, NAT, egress-only, transit gateway, peering, and endpoint targets, prefix lists) is
`external` with its AWS type; a reference that cannot be resolved is `unresolved` with a reason and
source path. When the operation that would have produced the target did not complete, the reason
says so and no local target is guessed.

## Limitations and redaction

- Failed operations are coverage failures; resources and facts that depend on them are omitted.
- If `DescribeSecurityGroupRules` did not complete, rules come from group permissions (no rule
  IDs) and the group records the fallback in `attributes.limitations`.
- Malformed values (invalid CIDR, port, or IPv6 address) are omitted with an
  `attributes.limitations` entry.
- Free-text `Description` values in network evidence and tag values under secret-named keys are
  redacted and listed in `redacted_paths`.
