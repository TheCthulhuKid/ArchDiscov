"""A session serving every collected service for account assembly, optionally partly denied."""

from botocore.exceptions import ClientError

from archdiscov.models.enums import AwsRegion
from tests.support.aws_fakes import Session as Ec2Session
from tests.support.ecs_data import Client as EcsClient
from tests.support.ecs_data import responses as ecs_responses
from tests.support.elbv2_data import Elbv2Client, ec2_pages
from tests.support.elbv2_data import responses as elbv2_responses
from tests.support.iam_data import PROFILE_ARN, IamClient
from tests.support.lambda_data import Client as LambdaClient
from tests.support.lambda_data import configuration
from tests.support.rds_data import Client as RdsClient
from tests.support.rds_data import data as rds_data
from tests.support.sqs_data import Client as SqsClient

REGIONS = (AwsRegion.US_WEST_2, AwsRegion.US_EAST_1)


def in_region(value, region):
    if isinstance(value, str):
        return value.replace("us-east-1", region.value)
    if isinstance(value, list):
        return [in_region(item, region) for item in value]
    if isinstance(value, dict):
        return {key: in_region(item, region) for key, item in value.items()}
    return value


def denied(operation):
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "assembly-private-value"}}, operation
    )


class Session:
    def __init__(self, partial):
        self.iam = IamClient()
        if partial:
            self.iam.data["get_role"] = denied("GetRole")
        self.clients = {}
        self.pages = {}
        for region in REGIONS:
            pages = ec2_pages(region)
            instance = pages["describe_instances"][0]["Reservations"][0]["Instances"][0]
            instance["IamInstanceProfile"] = {"Arn": PROFILE_ARN}
            if partial:
                pages["describe_subnets"].append(denied("DescribeSubnets"))
                pages["describe_route_tables"] = [denied("DescribeRouteTables")]
            self.pages[region] = pages
            elbv2 = in_region(elbv2_responses(), region)
            if partial:
                elbv2["describe_target_health"] = denied("DescribeTargetHealth")
            function = in_region(configuration(), region)
            self.clients[region] = {
                "elbv2": Elbv2Client(elbv2),
                "ecs": EcsClient(in_region(ecs_responses(), region)),
                "lambda": LambdaClient(
                    pages=[{"Functions": [function]}],
                    detail=denied("GetFunctionConfiguration") if partial else function,
                ),
                "rds": RdsClient(in_region(rds_data(), region)),
                "sqs": SqsClient({"list_queues": {"QueueUrls": []}}),
            }
        self.ec2 = Ec2Session(pages=lambda region: self.pages[region])

    def client(self, name, **kwargs):
        if name in {"sts", "ec2"}:
            return self.ec2.client(name, **kwargs)
        if name == "iam":
            return self.iam
        return self.clients[kwargs["region_name"]][name]
