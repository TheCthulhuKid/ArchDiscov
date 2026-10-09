import boto3
from botocore.stub import Stubber

from archdiscov.ingestion.elbv2.collector import collect_inventory
from archdiscov.ingestion.operations import Elbv2Operation as Op
from archdiscov.models.enums import AwsRegion, CoverageStatus

from .elbv2_data import GROUP, LB, LISTENER, RULE, Elbv2Session, config, context, responses


def test_real_stubber_all_six_reads_have_exact_request_scope():
    client = boto3.client(
        "elbv2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    session = Elbv2Session(client)
    data = responses()
    with Stubber(client) as stub:
        stub.add_response("describe_load_balancers", data["describe_load_balancers"], {})
        stub.add_response("describe_target_groups", data["describe_target_groups"], {})
        stub.add_response("describe_listeners", data["describe_listeners"], {"LoadBalancerArn": LB})
        stub.add_response("describe_rules", data["describe_rules"], {"ListenerArn": LISTENER})
        stub.add_response(
            "describe_target_health", data["describe_target_health"], {"TargetGroupArn": GROUP}
        )
        stub.add_response(
            "describe_tags",
            {
                "TagDescriptions": [
                    {"ResourceArn": arn, "Tags": [{"Key": "team", "Value": "infra"}]}
                    for arn in sorted([LB, LISTENER, RULE, GROUP])
                ]
            },
            {"ResourceArns": sorted([LB, LISTENER, RULE, GROUP])},
        )
        inventory = collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)
        stub.assert_no_pending_responses()
    assert set(inventory.results) == set(Op)
    assert all(item.status == CoverageStatus.SUCCESS for item in inventory.coverage)
    assert inventory.results[Op.DESCRIBE_TARGET_HEALTH].responses[0].path == (
        f'Request[{{"TargetGroupArn":"{GROUP}"}}].Page[0].TargetHealthDescriptions[0]'
    )
