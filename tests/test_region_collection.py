import pytest
from botocore.exceptions import ClientError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import EC2_OPERATIONS, AwsAdapter
from archdiscov.ingestion.operations import LambdaOperation
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.ingestion.scope import ResolvedAwsScope
from archdiscov.models.enums import AwsService
from archdiscov.services import collect_and_save, load_snapshot
from tests.aws_fakes import ACCOUNT, Clock, empty_network_pages
from tests.aws_fakes import Session as Ec2Session


class Session(Ec2Session):
    region_name = "us-east-1"

    def __init__(self, *, identity=ACCOUNT, discovery_error=None, unavailable=None):
        super().__init__(
            identity,
            lambda region: {
                "describe_vpcs": [
                    {"Vpcs": [{"VpcId": f"vpc-{region}", "CidrBlock": "10.0.0.0/16"}]}
                ],
                "describe_subnets": [{"Subnets": []}],
                "describe_instances": [{"Reservations": []}],
                **empty_network_pages(),
            },
        )
        self.calls = []
        self.discovery_error = discovery_error
        self.unavailable = unavailable

    def client(self, name, **kwargs):
        self.calls.append((name, kwargs.get("region_name")))
        if name == "sts":
            return self
        if name == "ec2":
            client = super().client(name, **kwargs)
            client.describe_regions = self.describe_regions
            return client
        raise AssertionError("an unavailable service must not create a client")

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/Reader"}

    def describe_regions(self, **params):
        self.calls.append(("DescribeRegions", params))
        if self.discovery_error:
            raise self.discovery_error
        return {
            "Regions": [
                {"RegionName": "us-west-2", "OptInStatus": "opted-in"},
                {"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"},
                {"RegionName": "ap-east-1", "OptInStatus": "not-opted-in"},
            ]
        }

    def get_available_regions(self, name, **kwargs):
        return [] if name == self.unavailable else ["us-east-1", "us-west-2"]


def request(**kwargs):
    data = {"profile": "dev", "expected_account_id": ACCOUNT, "region_scope": "enabled"}
    data.update(kwargs)
    return AwsCollectionRequest(accounts=(AwsAccountConfig(**data),))


def context(session):
    return AdapterContext(clock=Clock(), session_factory=lambda **kwargs: session)


def test_discovery_is_after_sts_and_resolved_scope_survives_persistence(tmp_path):
    session = Session()
    cfg = request()
    (outcome,) = collect_and_save(cfg, [tmp_path / "snapshot.json"], context=context(session))
    assert outcome.error is None and outcome.complete
    snapshot = load_snapshot(outcome.path)
    assert session.calls[0][0] == "sts"
    assert session.calls[2][0] == "DescribeRegions"
    scope = ResolvedAwsScope.model_validate(snapshot.metadata["collection_scope"])
    assert scope.region_scope == "enabled"
    assert snapshot.coverage.regions == list(scope.regions) == ["us-east-1", "us-west-2"]
    assert set(snapshot.regions) == set(scope.regions)
    assert snapshot.metadata["region_resolution"]["disabled_regions"] == ["ap-east-1"]
    assert len(snapshot.coverage.operations) == 2 * len(EC2_OPERATIONS)
    assert all(op.operation != "DescribeRegions" for op in snapshot.coverage.operations)
    assert cfg.accounts[0].regions == ()  # resolution never mutates caller configuration


def test_identity_mismatch_does_not_discover_regions_or_collect():
    session = Session(identity="000000000000")
    with pytest.raises(IngestionError) as exc:
        AwsAdapter().collect(request(), context=context(session))
    assert exc.value.code == "account_mismatch"
    assert session.calls == [("sts", "us-east-1")]


def test_identity_failure_does_not_discover_regions_or_publish(tmp_path):
    session = Session()

    def fail():
        raise RuntimeError("secret authentication text")

    session.get_caller_identity = fail
    path = tmp_path / "no-snapshot.json"
    (outcome,) = collect_and_save(request(), [path], context=context(session))
    assert outcome.error.code == "identity_failed"
    assert "secret authentication text" not in outcome.error.message
    assert session.calls == [("sts", "us-east-1")]
    assert not path.exists()


def test_explicit_override_never_calls_describe_regions():
    session = Session(discovery_error=AssertionError("must not discover"))
    (snapshot,) = AwsAdapter().collect(
        request(region_scope="explicit", regions=["us-west-2"]), context=context(session)
    )
    assert snapshot.coverage.regions == ["us-west-2"]
    assert all(name != "DescribeRegions" for name, _ in session.calls)


@pytest.mark.parametrize("service", ["ec2", "lambda", "elbv2", "ecs", "rds", "sqs"])
def test_unavailable_services_have_unsupported_not_successful_zero_coverage(tmp_path, service):
    selected = "elasticloadbalancing" if service == "elbv2" else service
    session = Session(unavailable=service)
    (outcome,) = collect_and_save(
        request(services=[selected]), [tmp_path / "partial.json"], context=context(session)
    )
    assert outcome.error is None and not outcome.complete
    snapshot = load_snapshot(outcome.path)
    assert snapshot.regions == {}
    assert snapshot.coverage.operations
    assert all(
        op.status == "unsupported" and op.result_count is None
        for op in snapshot.coverage.operations
    )
    assert all(name in {"sts", "ec2", "DescribeRegions"} for name, _ in session.calls)


def test_unavailable_service_preserves_successful_sibling_inventory(tmp_path):
    session = Session(unavailable="lambda")
    (outcome,) = collect_and_save(
        request(services=["ec2", "lambda"]), [tmp_path / "mixed.json"], context=context(session)
    )
    assert outcome.error is None and not outcome.complete
    snapshot = load_snapshot(outcome.path)
    assert snapshot.regions["us-east-1"].resources[0].identity.physical_id == "vpc-us-east-1"
    unavailable = [op for op in snapshot.coverage.operations if op.service is AwsService.LAMBDA]
    assert len(unavailable) == 2 * len(LambdaOperation)
    assert all(op.status == "unsupported" for op in unavailable)
    assert all(
        op.status == "success"
        for op in snapshot.coverage.operations
        if op.service is AwsService.EC2
    )


def test_discovery_failure_isolates_account_and_publishes_no_file(tmp_path):
    denied = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "secret"}}, "DescribeRegions"
    )
    bad, good = Session(discovery_error=denied), Session()
    first = request().accounts[0]
    second = first.model_copy(update={"expected_account_id": "210987654321", "profile": "other"})
    good.account = second.expected_account_id
    ctx = AdapterContext(
        clock=Clock(), session_factory=lambda profile_name: bad if profile_name == "dev" else good
    )
    paths = [tmp_path / "bad.json", tmp_path / "good.json"]
    outcomes = collect_and_save(AwsCollectionRequest(accounts=(first, second)), paths, context=ctx)
    assert outcomes[0].error.code == "region_discovery_failed"
    assert "secret" not in outcomes[0].error.message
    assert not paths[0].exists()
    assert outcomes[1].complete and paths[1].exists()
