from copy import deepcopy

from botocore.exceptions import ClientError

from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.ingestion.service_catalog import REGIONAL_OPERATIONS
from archdiscov.models.enums import AwsService
from archdiscov.models.resources.iam import IamRole
from archdiscov.services import collect_and_save, collect_snapshots, load_snapshot
from tests.support.aws_fakes import Session
from tests.support.iam_data import (
    IamClient,
    IamSession,
    config,
    context,
    ec2_pages,
    responses,
)


def combined_config():
    return config(regions=("us-east-1", "us-west-2")).model_copy(
        update={"services": (AwsService.EC2, AwsService.IAM)}
    )


def test_two_regions_collect_iam_once_and_links_round_trip(tmp_path):
    session = IamSession(ec2=Session(pages=ec2_pages))
    request = AwsCollectionRequest(accounts=(combined_config(),))
    path = tmp_path / "iam.json"
    (outcome,) = collect_and_save(request, [path], context=context(session))
    assert outcome.error is None
    snapshot = load_snapshot(path)
    assert snapshot == outcome.snapshot
    assert outcome.complete
    assert len(snapshot.global_resources) == 3
    # Each EC2 read in two regions, plus eight IAM reads.
    assert len(snapshot.coverage.operations) == 2 * len(REGIONAL_OPERATIONS[AwsService.EC2]) + 8
    assert sum(m == "list_roles" for m, _ in session.iam.calls) == 1
    assert sum(m == "list_instance_profiles" for m, _ in session.iam.calls) == 1
    assert len([name for name, _ in session.clients if name == "iam"]) == 1
    assert "protected-context" not in path.read_text()
    assert "protected-tag" not in path.read_text()
    for region in ("us-east-1", "us-west-2"):
        (link,) = snapshot.dependencies_by_region[region]
        assert link.attributes["kind"] == "instance_profile"
        assert link.target.kind == "local"
        cited = snapshot.evidence[link.evidence_ids[0]]
        assert cited.configuration_path == "Reservations[0].Instances[0].IamInstanceProfile.Arn"
        assert cited.resource_id == link.source_id
    kinds = {link.attributes["kind"] for link in snapshot.dependencies_by_region["global"]}
    assert kinds == {"profile_role", "role_policy", "permissions_boundary", "trust_principal"}


def test_iam_only_has_global_resources_and_no_regions():
    session = IamSession()
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(config(),)), context=context(session)
    )
    assert snapshot.regions == {}
    assert snapshot.coverage.account_wide
    assert snapshot.coverage.regions == []
    assert len(snapshot.coverage.operations) == 8
    role = next(r for r in snapshot.global_resources if isinstance(r, IamRole))
    assert (
        role.trust_document["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"]
        == "[REDACTED]"
    )
    assert role.inline_policies[0].name == "inline"


def test_failed_policy_and_role_details_preserve_known_configuration():
    data = responses()
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "malicious-secret"}}, "GetPolicy"
    )
    data["get_policy"] = denied
    data["get_role"] = denied
    session = IamSession(IamClient(data))
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(config(),)), context=context(session)
    )
    assert len(snapshot.global_resources) == 2
    role = next(r for r in snapshot.global_resources if isinstance(r, IamRole))
    assert role.trust_document is not None  # ListRoles trust survives GetRole failure
    assert role.attributes["attached_policy_arns"]
    links = snapshot.dependencies_by_region["global"]
    assert (
        next(link for link in links if link.attributes["kind"] == "role_policy").target.kind
        == "unresolved"
    )
    assert "malicious-secret" not in snapshot.model_dump_json()


def test_missing_profile_when_iam_not_selected_is_unresolved():
    session = IamSession(ec2=Session(pages=ec2_pages))
    account = combined_config().model_copy(update={"services": (AwsService.EC2,)})
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(account,)), context=context(session)
    )
    assert snapshot.global_resources == []
    assert session.iam.calls == []
    for region in account.regions:
        (link,) = snapshot.dependencies_by_region[region]
        assert link.target.kind == "unresolved"


def test_malformed_document_is_absent_and_coverage_is_qualified():
    data = responses()
    data["get_policy_version"]["PolicyVersion"]["Document"] = "%invalid-protected-value"
    session = IamSession(IamClient(deepcopy(data)))
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(config(),)), context=context(session)
    )
    policy = next(
        r for r in snapshot.global_resources if r.resource_type == "AWS::IAM::ManagedPolicy"
    )
    assert policy.document is None
    coverage = next(op for op in snapshot.coverage.operations if op.operation == "GetPolicyVersion")
    assert coverage.status == "incomplete"
    assert coverage.unresolved_values
    assert "invalid-protected-value" not in snapshot.model_dump_json()
