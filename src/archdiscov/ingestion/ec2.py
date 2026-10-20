"""Normalize EC2 VPC, subnet, and instance API output into resources with evidence."""

from typing import Any

from ..models.architecture import Ec2Instance, Subnet, Vpc
from .evidence_builder import EvidenceBuilder, tags_of
from .operations import Ec2Operation

VPC_OPERATION = Ec2Operation.DESCRIBE_VPCS
SUBNET_OPERATION = Ec2Operation.DESCRIBE_SUBNETS
INSTANCE_OPERATION = Ec2Operation.DESCRIBE_INSTANCES


class Ec2Normalizer(EvidenceBuilder):
    """Builds VPC, subnet, and instance resources from observed API items."""

    def vpc(self, index: int, item: dict[str, Any]) -> Vpc:
        tags = tags_of(item)
        cidrs = [item["CidrBlock"]] if item.get("CidrBlock") else []
        for association in item.get("CidrBlockAssociationSet") or []:
            cidr = association.get("CidrBlock")
            if cidr and cidr not in cidrs:
                cidrs.append(cidr)
        facts: dict[str, tuple[str, Any]] = {}
        if cidrs:
            facts["/cidr_blocks"] = ("CidrBlock", item.get("CidrBlock"))
        if "State" in item:
            facts["/attributes/state"] = ("State", item["State"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        return self._build(  # type: ignore[return-value]
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
            cidr_blocks=cidrs or None,
        )

    def subnet(self, index: int, item: dict[str, Any]) -> Subnet:
        tags = tags_of(item)
        facts: dict[str, tuple[str, Any]] = {}
        if item.get("CidrBlock"):
            facts["/cidr_blocks"] = ("CidrBlock", item["CidrBlock"])
        if item.get("AvailabilityZone"):
            facts["/availability_zone"] = ("AvailabilityZone", item["AvailabilityZone"])
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        return self._build(  # type: ignore[return-value]
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
            cidr_blocks=[item["CidrBlock"]] if item.get("CidrBlock") else None,
        )

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
