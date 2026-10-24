"""EC2 pages of one VPC, its subnets, and its instances, and a collection context over them."""

from datetime import UTC, datetime

from tests.support import aws_fakes as fakes
from tests.support.aws_fakes import ACCOUNT

VPC = "vpc-1"
VPCS = [
    {
        "VpcId": VPC,
        "CidrBlock": "10.0.0.0/16",
        "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}, {"CidrBlock": "10.1.0.0/16"}],
        "State": "available",
        "IsDefault": False,
        "Tags": [{"Key": "Name", "Value": "main"}],
    }
]
SUBNET = "subnet-1"
SUBNETS = [
    {
        "SubnetId": SUBNET,
        "VpcId": VPC,
        "CidrBlock": "10.0.1.0/24",
        "AvailabilityZone": "us-east-1a",
        "State": "available",
    },
    {"SubnetId": "subnet-orphan", "VpcId": "vpc-missing", "CidrBlock": "10.9.0.0/24"},
]
INSTANCE = "i-1"
RESERVATIONS = [
    {
        "Instances": [
            {
                "InstanceId": INSTANCE,
                "InstanceType": "t3.micro",
                "SubnetId": SUBNET,
                "VpcId": VPC,
                "State": {"Name": "running"},
                "LaunchTime": datetime(2025, 1, 1, tzinfo=UTC),
                "SecurityGroups": [{"GroupId": "sg-1", "GroupName": "web"}],
                "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "password", "Value": "hunter2"}],
            },
            {"InstanceId": "i-nosubnet", "InstanceType": "t3.nano", "SubnetId": "subnet-gone"},
        ]
    }
]


def default_pages(region):
    return {
        "describe_vpcs": [{"Vpcs": VPCS}],
        "describe_subnets": [{"Subnets": SUBNETS[:1]}, {"Subnets": SUBNETS[1:]}],
        "describe_instances": [{"Reservations": RESERVATIONS}],
        **fakes.empty_network_pages(),
    }


def ec2_context(pages=default_pages, account=ACCOUNT):
    return fakes.make_context(pages, account)
