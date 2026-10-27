# EC2 network collection mapping (step 13)

`archdiscov collect` reads EC2 network configuration with twelve extra operations. Each resource is
stored once; attachments are `attached_to` dependencies whose `attributes["kind"]` is an
`AttachmentKind`. Nothing here concludes reachability or internet exposure.

## Permissions

`ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:DescribeInstances`, `ec2:DescribeRouteTables`,
`ec2:DescribeInternetGateways`, `ec2:DescribeNetworkAcls`, `ec2:DescribeSecurityGroups`,
`ec2:DescribeSecurityGroupRules`, `ec2:DescribeNetworkInterfaces`, `ec2:DescribeNatGateways`,
`ec2:DescribeEgressOnlyInternetGateways`, `ec2:DescribeVpcPeeringConnections`,
`ec2:DescribeVpcEndpoints`, `ec2:DescribeManagedPrefixLists`, `ec2:GetManagedPrefixListEntries`
(plus `sts:GetCallerIdentity`, and `sts:AssumeRole` for configured roles). All are allowlisted in
`archdiscov.ingestion.operations` and rejected otherwise before any client call. Each region and
operation has one coverage record; zero results are `success`.

## Resources and facts

| Resource | Operation | Parent | Typed facts and source path |
| --- | --- | --- | --- |
| Route table | `DescribeRouteTables` | VPC (`VpcId`) | `/routes/N` ← `RouteTables[i].Routes[n]`, with IPv4, IPv6, or prefix-list destination (`prefix_list_id` when a prefix list) |
| Internet gateway | `DescribeInternetGateways` | VPC (`Attachments[0].VpcId`) | `/attributes/attached_vpc_ids` ← `Attachments` |
| Network ACL | `DescribeNetworkAcls` | VPC (`VpcId`) | `/entries/N` ← `NetworkAcls[i].Entries[n]` |
| Security group | `DescribeSecurityGroups` + `DescribeSecurityGroupRules` | VPC (`VpcId`) | `/ingress_rules/N`, `/egress_rules/N` ← `SecurityGroupRules[k]` (or `SecurityGroups[i].IpPermissions[j]` as fallback) |
| NAT gateway | `DescribeNatGateways` | Subnet (`SubnetId`), else VPC (a regional NAT gateway has no subnet) | `/state`, `/connectivity`, `/vpc_id`, `/subnet_id` ← same-named API fields; `/network_interface_id` ← the primary `NatGatewayAddresses[n].NetworkInterfaceId`; `/public_ips/N` ← `NatGatewayAddresses[n].PublicIp` |
| Egress-only internet gateway | `DescribeEgressOnlyInternetGateways` | VPC (`Attachments[0].VpcId`) | `/vpc_id` ← `EgressOnlyInternetGateways[i].Attachments[n]` |
| VPC peering connection | `DescribeVpcPeeringConnections` | Regional root (it belongs to neither VPC) | `/state` ← `Status`; `/requester`, `/accepter` ← `RequesterVpcInfo`, `AccepterVpcInfo` (VPC, owner, and the IPv4 and IPv6 CIDR sets, else the primary CIDR) |
| VPC endpoint | `DescribeVpcEndpoints` | VPC (`VpcId`) | `/endpoint_type`, `/service_name`, `/state`, `/vpc_id`, `/owner_id` ← same-named API fields; `/route_table_ids/N`, `/subnet_ids/N`, `/network_interface_ids/N` ← `VpcEndpoints[i].RouteTableIds[n]` and so on; `/security_group_ids/N` ← `VpcEndpoints[i].Groups[n]` |
| Managed prefix list | `DescribeManagedPrefixLists`, then one `GetManagedPrefixListEntries` per list | Regional root | `/address_family`, `/owner_id`, `/state`, `/version` ← same-named API fields; `/entries/N` ← the entries response's `Entries[n]`, only when every entry was read |
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
| `route_target` | route table → IGW / NAT gateway / egress-only internet gateway / network interface / instance / peering connection / endpoint (local); transit gateway → external | `RouteTables[i].Routes[n]` |
| `prefix_list_reference` | route table → prefix list a route's destination names, or security group → prefix list a rule names | the route or rule entry |
| `endpoint_route_table`, `endpoint_subnet`, `endpoint_interface`, `endpoint_security_group` | VPC endpoint → its route tables, subnets, network interfaces, and security groups | `VpcEndpoints[i].RouteTableIds[n]`, `...SubnetIds[n]`, `...NetworkInterfaceIds[n]`, `...Groups[n]` |
| `internet_gateway_attachment` | IGW → VPC | `InternetGateways[i].Attachments[n]` |
| `egress_only_internet_gateway_attachment` | egress-only internet gateway → VPC, with the attachment `state` | `EgressOnlyInternetGateways[i].Attachments[n]` |
| `peering_requester_vpc`, `peering_accepter_vpc` | peering connection → each side's VPC; local or unresolved in this account and region, else external with the side's account and region | `VpcPeeringConnections[i].RequesterVpcInfo`, `...AccepterVpcInfo` |
| `nat_gateway_subnet`, `nat_gateway_interface` | NAT gateway → subnet, and → network interface | `NatGateways[i].SubnetId`, `NatGateways[i].NatGatewayAddresses[n].NetworkInterfaceId` |
| `network_acl_association` | NACL → subnet | `NetworkAcls[i].Associations[n]` |
| `instance_interface` | network interface → instance | `NetworkInterfaces[i].Attachment` |
| `security_group_membership` | instance or interface → security group | `...SecurityGroups[n]` / `NetworkInterfaces[i].Groups[n]` |
| `security_group_reference` | security group → referenced group (local, or external when another account) | the rule entry |

Target kinds: a stored resource is `local`; a known resource outside the collection (other account's
group, the far side of a cross-account or cross-region peering connection, transit gateway targets)
is `external` with its AWS type; a reference that cannot be resolved is `unresolved` with a reason
and source path. When the operation that would have produced the target did not complete, the reason
says so and no local target is guessed.

## Limitations and redaction

- Failed operations are coverage failures; resources and facts that depend on them are omitted.
- If `DescribeSecurityGroupRules` did not complete, rules come from group permissions (no rule
  IDs) and the group records the fallback in `attributes.limitations`.
- Malformed values (invalid CIDR, port, or IP address) are omitted with an
  `attributes.limitations` entry. A NAT gateway whose connectivity contradicts its addresses (an
  available public gateway without one, or a private gateway with one) is recorded without
  `connectivity`, with a limitation saying why.
- NAT gateways are recorded in every reported state, including `deleted` and `failed`.
- A VPC endpoint of a type the model does not know (such as `Resource` or `ServiceNetwork`) is
  recorded without `endpoint_type`, with a limitation.
- Each prefix list's entries are read with one `GetManagedPrefixListEntries` call of at most 100
  entries (the API's largest page). A list with more, a denied or failed read, or any invalid entry
  leaves `entries` unset (not recorded), never a partial list; the first two mark that operation's
  coverage incomplete. With no list collected, the entries read is `skipped`.
- Free-text `Description` values in network evidence and tag values under secret-named keys are
  redacted and listed in `redacted_paths`.
