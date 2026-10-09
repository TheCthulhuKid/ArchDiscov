from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsService

ACCOUNT = "123456789012"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/web"
PROFILE_ARN = f"arn:aws:iam::{ACCOUNT}:instance-profile/web"
POLICY_ARN = "arn:aws:iam::aws:policy/ReadOnlyAccess"
TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Condition": {"StringEquals": {"sts:ExternalId": "protected-context"}},
        }
    ],
}
ROLE = {
    "RoleId": "AROAEXAMPLE",
    "RoleName": "web",
    "Arn": ROLE_ARN,
    "AssumeRolePolicyDocument": TRUST,
    "PermissionsBoundary": {"PermissionsBoundaryArn": POLICY_ARN},
    "Tags": [{"Key": "password", "Value": "protected-tag"}],
}
PROFILE = {
    "InstanceProfileId": "AIPAEXAMPLE",
    "InstanceProfileName": "web",
    "Arn": PROFILE_ARN,
    "Roles": [ROLE],
}
POLICY = {
    "PolicyId": "ANPAEXAMPLE",
    "PolicyName": "ReadOnlyAccess",
    "Arn": POLICY_ARN,
    "DefaultVersionId": "v7",
}
DOCUMENT = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}


def ec2_pages(region):
    from .aws_fakes import empty_network_pages

    return {
        **empty_network_pages(),
        "describe_vpcs": [{"Vpcs": []}],
        "describe_subnets": [{"Subnets": []}],
        "describe_instances": [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-web", "IamInstanceProfile": {"Arn": PROFILE_ARN}}
                        ]
                    }
                ]
            }
        ],
    }


def responses():
    return deepcopy(
        {
            "list_roles": {"Roles": [ROLE]},
            "list_instance_profiles": {"InstanceProfiles": [PROFILE]},
            "get_role": {"Role": ROLE},
            "list_attached_role_policies": {
                "AttachedPolicies": [{"PolicyName": POLICY["PolicyName"], "PolicyArn": POLICY_ARN}]
            },
            "list_role_policies": {"PolicyNames": ["inline"]},
            "get_role_policy": {
                "RoleName": "web",
                "PolicyName": "inline",
                "PolicyDocument": DOCUMENT,
            },
            "get_policy": {"Policy": POLICY},
            "get_policy_version": {"PolicyVersion": {"VersionId": "v7", "Document": DOCUMENT}},
        }
    )


class IamClient:
    def __init__(self, data=None):
        self.data = responses() if data is None else data
        self.calls = []
        self.lock = Lock()

    def __getattr__(self, method):
        def call(**params):
            with self.lock:
                self.calls.append((method, params))
            result = self.data[method]
            if callable(result):
                result = result(params)
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)

        return call


class IamSession:
    def __init__(self, iam=None, account=ACCOUNT, ec2=None):
        self.iam = iam or IamClient()
        self.account = account
        self.ec2 = ec2
        self.clients = []

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
        if name == "iam":
            return self.iam
        if name == "sts":
            return self
        return self.ec2.client(name, **kwargs)

    def get_caller_identity(self):
        return {"Account": self.account}


def config(**kwargs):
    return AwsAccountConfig(
        profile="test", expected_account_id=ACCOUNT, services=(AwsService.IAM,), **kwargs
    )


def context(session):
    return AdapterContext(
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
        uuid_factory=lambda: UUID(int=14),
        session_factory=lambda **kwargs: session,
    )
