"""Normalize EC2 VPC, subnet, and instance API output into resources with evidence."""

from typing import Any

from ..models.architecture import Ec2Instance, Subnet, Vpc
from .evidence_builder import EvidenceBuilder, tags_of
from .operations import Ec2Operation

VPC_OPERATION = Ec2Operation.DESCRIBE_VPCS
SUBNET_OPERATION = Ec2Operation.DESCRIBE_SUBNETS
INSTANCE_OPERATION = Ec2Operation.DESCRIBE_INSTANCES


def _associated_ipv6(item: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """IPv6 CIDR associations in the `associated` state, with their source positions."""
    return [
        (i, association)
        for i, association in enumerate(item.get("Ipv6CidrBlockAssociationSet") or [])
        if association.get("Ipv6CidrBlock")
        and (association.get("Ipv6CidrBlockState") or {}).get("State") == "associated"
    ]


class Ec2Normalizer(EvidenceBuilder):
    """Builds VPC, subnet, and instance resources from observed API items."""

    def _cite_ipv6(
        self,
        resource: Vpc | Subnet,
        operation: str,
        path: str,
        ipv4_count: int,
        associations: list[tuple[int, dict[str, Any]]],
    ) -> None:
        """Cite each IPv6 CIDR, which follows the IPv4 CIDRs, to its association entry."""
        for n, (i, association) in enumerate(associations):
            self.cite(
                resource,
                f"/cidr_blocks/{ipv4_count + n}",
                operation,
                f"{path}.Ipv6CidrBlockAssociationSet[{i}]",
                association,
            )

    def vpc(self, index: int, item: dict[str, Any]) -> Vpc:
        tags = tags_of(item)
        cidrs = [item["CidrBlock"]] if item.get("CidrBlock") else []
        for association in item.get("CidrBlockAssociationSet") or []:
            cidr = association.get("CidrBlock")
            if cidr and cidr not in cidrs:
                cidrs.append(cidr)
        ipv6 = _associated_ipv6(item)
        facts: dict[str, tuple[str, Any]] = {}
        if cidrs:
            facts["/cidr_blocks"] = ("CidrBlock", item.get("CidrBlock"))
        if "State" in item:
            facts["/attributes/state"] = ("State", item["State"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        vpc: Vpc = self._build(  # type: ignore[assignment]
            Vpc,
            operation=VPC_OPERATION,
            item_path=f"Vpcs[{index}]",
            item=item,
            physical_id=item["VpcId"],
            name=tags.get("Name"),
            attributes={
                "state": item.get("State"),
                "is_default": item.get("IsDefault"),
                "owner_id": item.get("OwnerId"),
                "instance_tenancy": item.get("InstanceTenancy"),
                "tags": tags or None,
            },
            facts=facts,
            cidr_blocks=[*cidrs, *(a["Ipv6CidrBlock"] for _, a in ipv6)] or None,
        )
        self._cite_ipv6(vpc, VPC_OPERATION, f"Vpcs[{index}]", len(cidrs), ipv6)
        return vpc

    def subnet(self, index: int, item: dict[str, Any]) -> Subnet:
        tags = tags_of(item)
        cidrs = [item["CidrBlock"]] if item.get("CidrBlock") else []
        ipv6 = _associated_ipv6(item)
        facts: dict[str, tuple[str, Any]] = {}
        if cidrs:
            facts["/cidr_blocks"] = ("CidrBlock", item["CidrBlock"])
        if item.get("AvailabilityZone"):
            facts["/availability_zone"] = ("AvailabilityZone", item["AvailabilityZone"])
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        subnet: Subnet = self._build(  # type: ignore[assignment]
            Subnet,
            operation=SUBNET_OPERATION,
            item_path=f"Subnets[{index}]",
            item=item,
            physical_id=item["SubnetId"],
            name=tags.get("Name"),
            attributes={
                "state": item.get("State"),
                "vpc_id": item.get("VpcId"),
                "map_public_ip_on_launch": item.get("MapPublicIpOnLaunch"),
                "tags": tags or None,
            },
            facts=facts,
            availability_zone=item.get("AvailabilityZone"),
            cidr_blocks=[*cidrs, *(a["Ipv6CidrBlock"] for _, a in ipv6)] or None,
        )
        self._cite_ipv6(subnet, SUBNET_OPERATION, f"Subnets[{index}]", len(cidrs), ipv6)
        return subnet

    def instance(self, path: str, item: dict[str, Any]) -> Ec2Instance:
        tags = tags_of(item)
        state = (item.get("State") or {}).get("Name")
        profile = (item.get("IamInstanceProfile") or {}).get("Arn")
        groups = [g["GroupId"] for g in item.get("SecurityGroups") or [] if "GroupId" in g]
        zone = (item.get("Placement") or {}).get("AvailabilityZone")
        facts: dict[str, tuple[str, Any]] = {}
        if item.get("InstanceType"):
            facts["/instance_type"] = ("InstanceType", item["InstanceType"])
        if state:
            facts["/attributes/state"] = ("State.Name", state)
        if item.get("SubnetId"):
            facts["/attributes/subnet_id"] = ("SubnetId", item["SubnetId"])
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        if item.get("PrivateIpAddress"):
            facts["/attributes/private_ip_address"] = ("PrivateIpAddress", item["PrivateIpAddress"])
        if item.get("PublicIpAddress"):
            facts["/attributes/public_ip_address"] = ("PublicIpAddress", item["PublicIpAddress"])
        if groups:
            facts["/attributes/security_group_ids"] = ("SecurityGroups", item["SecurityGroups"])
        if profile:
            facts["/attributes/iam_instance_profile_arn"] = ("IamInstanceProfile.Arn", profile)
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        return self._build(  # type: ignore[return-value]
            Ec2Instance,
            operation=INSTANCE_OPERATION,
            item_path=path,
            item=item,
            physical_id=item["InstanceId"],
            name=tags.get("Name"),
            attributes={
                "state": state,
                "vpc_id": item.get("VpcId"),
                "subnet_id": item.get("SubnetId"),
                "image_id": item.get("ImageId"),
                "private_ip_address": item.get("PrivateIpAddress"),
                "public_ip_address": item.get("PublicIpAddress"),
                "availability_zone": zone,
                "security_group_ids": groups or None,
                "iam_instance_profile_arn": profile,
                "tags": tags or None,
            },
            facts=facts,
            instance_type=item.get("InstanceType"),
        )
