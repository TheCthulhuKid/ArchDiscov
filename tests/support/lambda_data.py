"""A Lambda function's configuration and a fake Lambda client that serves it."""

from copy import deepcopy

from archdiscov.models.enums import AwsRegion

REGION = AwsRegion.US_EAST_1
ACCOUNT = "123456789012"
ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"
SECRET = "injected-private-value"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/worker"


def configuration(arn=ARN):
    return {
        "FunctionArn": arn,
        "FunctionName": arn.rsplit(":", 1)[-1],
        "Role": ROLE,
        "Runtime": "python3.13",
        "PackageType": "Zip",
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "MemorySize": 256,
        "Timeout": 30,
        "Architectures": ["arm64"],
        "VpcConfig": {"VpcId": "vpc-a", "SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]},
    }


class Client:
    def __init__(self, pages=None, detail=None):
        self.pages = [{"Functions": [configuration()]}] if pages is None else pages
        self.detail = configuration() if detail is None else detail
        self.calls = []

    def list_functions(self, **params):
        self.calls.append(("list_functions", params))
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def get_function_configuration(self, **params):
        self.calls.append(("get_function_configuration", params))
        if isinstance(self.detail, Exception):
            raise self.detail
        return deepcopy(self.detail)
