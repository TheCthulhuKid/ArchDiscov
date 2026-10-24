from copy import deepcopy
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsService
from tests.support.aws_fakes import empty_network_pages

from .aws_fakes import ACCOUNT, Clock, Session

REGION = "us-east-1"
PREFIX = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:"
LB = PREFIX + "loadbalancer/app/web/1234567890abcdef"
NLB = PREFIX + "loadbalancer/net/tcp/1234567890abcdef"
LISTENER = PREFIX + "listener/app/web/1234567890abcdef/abcdef1234567890"
RULE = PREFIX + "listener-rule/app/web/1234567890abcdef/abcdef1234567890/1111111111111111"
GROUP = PREFIX + "targetgroup/web/1234567890abcdef"


def config(**overrides):
    return AwsAccountConfig(
        **{
            "profile": "dev",
            "expected_account_id": ACCOUNT,
            "regions": (REGION,),
            "services": (AwsService.ELASTIC_LOAD_BALANCING,),
            "max_concurrency": 1,
            **overrides,
        }
    )


def responses():
    return deepcopy(
        {
            "describe_load_balancers": {
                "LoadBalancers": [
                    {
                        "LoadBalancerArn": LB,
                        "LoadBalancerName": "web",
                        "Type": "application",
                        "Scheme": "internal",
                        "DNSName": "web.elb.example",
                        "IpAddressType": "ipv4",
                        "VpcId": "vpc-a",
                        "SecurityGroups": ["sg-a"],
                        "AvailabilityZones": [{"ZoneName": "us-east-1a", "SubnetId": "subnet-a"}],
                        "State": {"Code": "active"},
                    }
                ]
            },
            "describe_target_groups": {
                "TargetGroups": [
                    {
                        "TargetGroupArn": GROUP,
                        "TargetGroupName": "web",
                        "VpcId": "vpc-a",
                        "TargetType": "instance",
                        "Protocol": "HTTP",
                        "Port": 8080,
                        "HealthCheckProtocol": "HTTP",
                        "HealthCheckPort": "traffic-port",
                    }
                ]
            },
            "describe_listeners": {
                "Listeners": [
                    {
                        "ListenerArn": LISTENER,
                        "LoadBalancerArn": LB,
                        "Port": 80,
                        "Protocol": "HTTP",
                        "DefaultActions": [
                            {
                                "Type": "forward",
                                "Order": 1,
                                "ForwardConfig": {
                                    "TargetGroups": [{"TargetGroupArn": GROUP, "Weight": 100}],
                                },
                            }
                        ],
                    }
                ]
            },
            "describe_rules": {
                "Rules": [
                    {
                        "RuleArn": RULE,
                        "Priority": "1",
                        "IsDefault": False,
                        "Conditions": [{"Field": "path-pattern", "Values": ["/api/*"]}],
                        "Actions": [{"Type": "forward", "TargetGroupArn": GROUP, "Order": 1}],
                    }
                ]
            },
            "describe_target_health": {
                "TargetHealthDescriptions": [
                    {
                        "Target": {"Id": "i-web", "Port": 8080, "AvailabilityZone": "us-east-1a"},
                        "TargetHealth": {
                            "State": "unhealthy",
                            "Reason": "Target.FailedHealthChecks",
                        },
                    }
                ]
            },
        }
    )


class Elbv2Client:
    def __init__(self, data=None):
        self.data = responses() if data is None else data
        self.calls = []

    def __getattr__(self, method):
        def call(**params):
            self.calls.append((method, deepcopy(params)))
            if method == "describe_tags" and method not in self.data:
                return {
                    "TagDescriptions": [
                        {"ResourceArn": arn, "Tags": [{"Key": "team", "Value": "infra"}]}
                        for arn in params["ResourceArns"]
                    ]
                }
            value = self.data[method]
            if callable(value):
                value = value(params)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        return call


class Elbv2Session:
    def __init__(self, client=None, ec2=None, iam=None, account=ACCOUNT):
        self.elb = client or Elbv2Client()
        self.ec2 = ec2
        self.iam = iam
        self.account = account
        self.clients = []

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
        if name == "elbv2":
            return self.elb
        if name == "iam":
            return self.iam
        return (self.ec2 or Session(account=self.account)).client(name, **kwargs)


def context(session):
    return AdapterContext(
        clock=Clock(),
        uuid_factory=lambda: UUID(int=15),
        session_factory=lambda **kw: session,
    )


def ec2_pages(region):
    return {
        **empty_network_pages(),
        "describe_vpcs": [{"Vpcs": [{"VpcId": "vpc-a"}]}],
        "describe_subnets": [{"Subnets": [{"SubnetId": "subnet-a", "VpcId": "vpc-a"}]}],
        "describe_instances": [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-web", "SubnetId": "subnet-a", "VpcId": "vpc-a"}
                        ]
                    }
                ]
            }
        ],
        "describe_security_groups": [{"SecurityGroups": [{"GroupId": "sg-a", "VpcId": "vpc-a"}]}],
    }
