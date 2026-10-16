from uuid import UUID

from archdiscov.ingestion.configuration_relationships import merge_resources, resolve_relationships
from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.models.architecture import ResourceIdentity, SecurityGroup, Vpc
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.functions import LambdaFunction
from archdiscov.models.resources.iam import IamRole

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/runner"
FUNCTION = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:worker"


def identity(service, ref, region=AwsRegion.US_EAST_1):
    return ResourceIdentity(account_id=ACCOUNT, region=region, service=service, physical_id=ref)


def test_exact_source_owned_network_and_global_role_links():
    builder = EvidenceBuilder(UUID(int=1), ACCOUNT, AwsRegion.US_EAST_1, AwsService.LAMBDA)
    function = LambdaFunction(
        identity=identity(AwsService.LAMBDA, FUNCTION),
        arn=FUNCTION,
        role_arn=ROLE,
        vpc_id="vpc-a",
        security_group_ids=["sg-a"],
    )
    builder.cite(function, "/role_arn", "GetFunctionConfiguration", "Function.Role", ROLE)
    builder.cite(
        function,
        "/security_group_ids/0",
        "GetFunctionConfiguration",
        "Function.VpcConfig.SecurityGroupIds[0]",
        "sg-a",
    )
    role = IamRole(identity=identity(AwsService.IAM, "role-id", AwsRegion.GLOBAL), arn=ROLE)
    group = SecurityGroup(identity=identity(AwsService.EC2, "sg-a"))
    vpc = Vpc(identity=identity(AwsService.EC2, "vpc-a"), children=[group])
    roots = [vpc]
    merge_resources(roots, [function])
    assert function in vpc.children
    links = resolve_relationships([*roots, role], builder.evidence)[AwsRegion.US_EAST_1]
    assert [link.target.resource_id for link in links] == [role.id, group.id]
    assert all(
        builder.evidence[eid].resource_id == function.id
        for link in links
        for eid in link.evidence_ids
    )


def test_same_scope_missing_external_and_cross_region_id_collision():
    builder = EvidenceBuilder(UUID(int=1), ACCOUNT, AwsRegion.US_EAST_1, AwsService.LAMBDA)
    external = "arn:aws:iam::999999999999:role/external"
    function = LambdaFunction(
        identity=identity(AwsService.LAMBDA, FUNCTION),
        arn=FUNCTION,
        role_arn=external,
        subnet_ids=["subnet-missing"],
        security_group_ids=["sg-a"],
    )
    for field, value in [
        ("role_arn", external),
        ("subnet_ids/0", "subnet-missing"),
        ("security_group_ids/0", "sg-a"),
    ]:
        builder.cite(function, f"/{field}", "GetFunctionConfiguration", f"Function.{field}", value)
    other_region_group = SecurityGroup(
        identity=identity(AwsService.EC2, "sg-a", AwsRegion.US_WEST_2)
    )
    links = resolve_relationships([function, other_region_group], builder.evidence)[
        AwsRegion.US_EAST_1
    ]
    assert [link.target.kind for link in links] == ["external", "unresolved", "unresolved"]
    assert links[0].target.account_id == "999999999999"
    assert all(link.target.source_path.startswith("Function.") for link in links[1:])
