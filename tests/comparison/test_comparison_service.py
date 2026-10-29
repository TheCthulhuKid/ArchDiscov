import json
from datetime import UTC, datetime

import pytest

from archdiscov.ingestion.cloudformation import CloudFormationRequest
from archdiscov.ingestion.terraform import TerraformStateRequest
from archdiscov.services import (
    ComparisonValidationError,
    SnapshotConflictError,
    SnapshotIOError,
    SnapshotJSONError,
    collect_snapshots,
    compare_snapshots,
    load_comparison,
    save_comparison,
    validate_comparison_result,
)
from tests.comparison.snapshots import aws_resource, aws_snapshot, coverage
from tests.paths import FIXTURES_ROOT
from tests.support.aws_fakes import empty_network_pages, make_context, request
from tests.support.comparison_data import rid, vpc
from tests.support.network_data import SUBNET, element_pages
from tests.support.terraform_data import CONTEXT, plan_request

NOW = datetime(2026, 2, 1, tzinfo=UTC)
DELETE = {"actions": ["delete"], "value_scope": "before"}


def snapshots(left, right, *, sources=("aws", "aws"), left_kw=None, right_kw=None):
    return (
        aws_snapshot(left, sources[0], seed=1, **(left_kw or {})),
        aws_snapshot(right, sources[1], seed=2, **(right_kw or {})),
    )


def compare(left, right, **options):
    limit = options.pop("result_limit", 1000)
    first, second = snapshots(left, right, **options)
    return compare_snapshots(first, second, result_limit=limit, now=NOW), first, second


# ---- observed, state, plan, and CloudFormation pairs -------------------------------


def test_observed_pairs_report_matched_differences_and_unmatched_resources_per_side():
    result, *_ = compare(
        [vpc("vpc-1", attributes={"a": 1}), vpc("vpc-2")],
        [vpc("vpc-1", attributes={"a": 2}), vpc("vpc-3")],
    )
    [pair] = result.matched
    assert [d.path for d in pair.differences] == ["/attributes/a"]
    assert {(item.side, item.resource.resource_id) for item in result.unmatched} == {
        ("left", rid("vpc-2")),
        ("right", rid("vpc-3")),
    }
    assert result.limitations == []
    assert result.left.snapshot_id.int == 1 and result.right.snapshot_id.int == 2


def test_observed_against_terraform_state_matches_logical_addresses_with_a_limitation():
    address = "module.net.aws_vpc.main"
    result, *_ = compare(
        [vpc("vpc-1", logical=address)],
        [aws_resource(logical=address, physical="vpc-1")],
        sources=("aws", "terraform_state"),
    )
    [pair] = result.matched
    assert pair.basis == "physical_identity"
    assert [str(item.kind) for item in result.limitations] == ["source_kind_mismatch"]
    assert str(result.right.source_kind) == "terraform_state"


def test_observed_against_a_plan_reports_a_planned_deletion_not_an_absence():
    plan = {**vpc("vpc-1"), "lifecycle": DELETE}
    result, *_ = compare([vpc("vpc-1")], [plan], sources=("aws", "terraform_plan"))
    [pair] = result.matched
    assert [str(q.kind) for q in pair.qualifications] == ["planned_lifecycle"]
    assert result.unmatched == []


def test_observed_against_cloudformation_pairs_declared_and_observed_resources():
    result, *_ = compare(
        [vpc("vpc-1", attributes={"a": 1})],
        [vpc("vpc-1", attributes={"a": 2}), vpc("vpc-9")],
        sources=("aws", "cloudformation"),
        right_kw={"operations": [coverage("incomplete")]},
    )
    assert len(result.matched) == 1 and len(result.unmatched) == 1
    assert str(result.right.source_kind) == "cloudformation"
    assert any(
        str(q.kind) == "limited_coverage" for q in result.matched[0].differences[0].qualifications
    )


def test_a_different_scope_matches_nothing():
    other = aws_snapshot(
        [aws_resource(physical="vpc-1", account="999999999999")], seed=2, account="999999999999"
    )
    first = aws_snapshot([vpc("vpc-1")], seed=1)
    result = compare_snapshots(first, other, now=NOW)
    assert result.matched == [] and len(result.unmatched) == 2
    assert [str(item.kind) for item in result.limitations] == ["scope_mismatch"]


def test_comparison_is_deterministic_and_does_not_change_its_inputs():
    first, second = snapshots(
        [vpc("vpc-1", attributes={"a": 1})], [vpc("vpc-1", attributes={"a": 2})]
    )
    before = (first.model_dump_json(), second.model_dump_json())
    one = compare_snapshots(first, second, now=NOW)
    two = compare_snapshots(first, second, now=NOW)
    assert one == two and one.model_dump_json() == two.model_dump_json()
    assert (first.model_dump_json(), second.model_dump_json()) == before


# ---- limits ------------------------------------------------------------------------


def test_each_kind_of_entry_is_truncated_with_a_limitation():
    result, *_ = compare(
        [vpc(f"left-{n}") for n in range(3)],
        [vpc(f"right-{n}") for n in range(3)],
        result_limit=2,
    )
    assert [item.side for item in result.unmatched].count("left") == 2
    assert [item.side for item in result.unmatched].count("right") == 2
    assert [str(item.kind) for item in result.limitations] == ["result_limit_reached"]


def test_a_result_within_its_limit_is_not_truncated():
    result, *_ = compare([vpc("a")], [vpc("b")], result_limit=1)
    assert result.limitations == []


@pytest.mark.parametrize("limit", [0, 10001])
def test_the_result_limit_is_bounded(limit):
    first, second = snapshots([vpc()], [vpc()])
    with pytest.raises(ValueError, match="result_limit"):
        compare_snapshots(first, second, result_limit=limit)


# ---- persistence and integrity -----------------------------------------------------


def saved(tmp_path, **options):
    result, first, second = compare(
        [vpc("vpc-1", attributes={"a": 1}), vpc("vpc-2")],
        [vpc("vpc-1", attributes={"a": 2})],
        **options,
    )
    path = save_comparison(result, tmp_path / "comparison.json", left=first, right=second)
    return result, first, second, path


def test_a_saved_comparison_reloads_and_verifies(tmp_path):
    result, first, second, path = saved(tmp_path)
    assert load_comparison(path, left=first, right=second) == result
    assert path.read_text().endswith("\n")


def test_an_existing_destination_is_never_overwritten(tmp_path):
    result, first, second, path = saved(tmp_path)
    original = path.read_text()
    with pytest.raises(SnapshotConflictError):
        save_comparison(result, path, left=first, right=second)
    assert path.read_text() == original


def test_swapped_or_wrong_snapshots_are_rejected(tmp_path):
    _result, first, second, path = saved(tmp_path)
    with pytest.raises(ComparisonValidationError) as swapped:
        load_comparison(path, left=second, right=first)
    assert [issue.type for issue in swapped.value.details] == ["snapshot_mismatch"]
    assert swapped.value.path == path


def test_a_changed_snapshot_with_the_same_id_fails_verification(tmp_path):
    _result, first, _second, path = saved(tmp_path)
    changed = aws_snapshot([vpc("vpc-1", attributes={"a": 5})], seed=2)
    with pytest.raises(ComparisonValidationError) as error:
        load_comparison(path, left=first, right=changed)
    assert {issue.location[0] for issue in error.value.details} >= {"matched"}


def rewrite(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda d: d["matched"][0]["differences"][0].update(right_value=99), "matched"),
        (lambda d: d.update(unmatched=[]), "unmatched"),
        (lambda d: d["left"].update(source_kind="mock"), "left"),
        (
            lambda d: d.update(limitations=[{"kind": "cross_cloud", "message": "made up"}]),
            "limitations",
        ),
    ],
    ids=["matched", "unmatched", "left", "limitations"],
)
def test_tampered_results_are_rejected(tmp_path, change, field):
    _, first, second, path = saved(tmp_path)
    rewrite(path, change)
    with pytest.raises(ComparisonValidationError) as error:
        load_comparison(path, left=first, right=second)
    assert error.value.code == "invalid_comparison_result"
    assert field in {issue.location[0] for issue in error.value.details}


def test_raising_the_recorded_limit_of_a_truncated_result_is_rejected(tmp_path):
    result, first, second = compare([vpc(f"left-{n}") for n in range(3)], [], result_limit=1)
    path = save_comparison(result, tmp_path / "truncated.json", left=first, right=second)
    rewrite(path, lambda d: d.update(result_limit=5))
    with pytest.raises(ComparisonValidationError):
        load_comparison(path, left=first, right=second)
    rewrite(path, lambda d: d.update(result_limit=1, limitations=[]))
    with pytest.raises(ComparisonValidationError):
        load_comparison(path, left=first, right=second)


def test_a_truncated_result_verifies_against_its_recorded_limit(tmp_path):
    result, first, second, path = saved(tmp_path, result_limit=1)
    assert len(result.matched) == 1
    assert load_comparison(path, left=first, right=second).result_limit == 1


def test_invalid_results_fail_with_safe_diagnostics(tmp_path):
    _, first, second, path = saved(tmp_path)
    rewrite(path, lambda d: d.update(secret_key="hunter2"))
    with pytest.raises(ComparisonValidationError) as error:
        load_comparison(path, left=first, right=second)
    rendered = repr(error.value.details)
    assert "hunter2" not in rendered and "secret_key" not in rendered
    assert error.value.details[0].location == ("field",)


def test_unreadable_inputs_fail_like_other_result_files(tmp_path):
    _, first, second, path = saved(tmp_path)
    with pytest.raises(SnapshotIOError):
        load_comparison(tmp_path / "missing.json", left=first, right=second)
    path.write_text("{not json")
    with pytest.raises(SnapshotJSONError):
        load_comparison(path, left=first, right=second)


def test_an_unverifiable_result_is_never_written(tmp_path):
    result, first, second = compare([vpc("vpc-1")], [vpc("vpc-1")])
    other = aws_snapshot([vpc("vpc-1")], seed=9)
    target = tmp_path / "never.json"
    with pytest.raises(ComparisonValidationError) as error:
        save_comparison(result, target, left=first, right=other)
    assert error.value.path == target
    assert not target.exists()
    assert validate_comparison_result(result, first, second) == result


# ---- phase 6 network elements --------------------------------------------------------------


def test_collected_network_elements_match_their_declarations_by_identity_only(tmp_path):
    region, account = "us-east-1", "123456789012"
    observed = collect_snapshots(
        request(account),
        context=make_context(
            lambda _: {
                **empty_network_pages(),
                "describe_vpcs": [{"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"}]}],
                "describe_subnets": [{"Subnets": [SUBNET]}],
                "describe_instances": [{"Reservations": []}],
                **element_pages(),
            }
        ),
    )[0]
    declared = {
        "aws_nat_gateway.public": {"id": "nat-1", "subnet_id": "subnet-1", "public_ip": ""},
        "aws_egress_only_internet_gateway.main": {"id": "eigw-1", "vpc_id": "vpc-1"},
        "aws_vpc_peering_connection.peer": {
            "id": "pcx-1",
            "vpc_id": "vpc-1",
            "peer_vpc_id": "vpc-2",
        },
        "aws_vpc_endpoint.s3": {"id": "vpce-1", "vpc_endpoint_type": "Gateway", "vpc_id": "vpc-1"},
        "aws_ec2_managed_prefix_list.office": {"id": "pl-office", "address_family": "IPv4"},
        "aws_ec2_transit_gateway.main": {"id": "tgw-1"},
        "aws_ec2_transit_gateway_vpc_attachment.app": {"id": "tgw-attach-1", "vpc_id": "vpc-1"},
        "aws_ec2_transit_gateway_route_table.main": {"id": "tgw-rtb-1"},
    }
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "format_version": "1.0",
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "address": address,
                                "mode": "managed",
                                "type": address.split(".")[0],
                                "provider_name": "registry.terraform.io/hashicorp/aws",
                                "values": values,
                                "sensitive_values": {},
                            }
                            for address, values in declared.items()
                        ]
                    }
                },
            }
        )
    )
    imported = collect_snapshots(
        TerraformStateRequest(path=state, account_id=account, region=region), context=CONTEXT
    )[0]
    result = compare_snapshots(observed, imported, result_limit=1000, now=NOW)
    elements = {f"{account}/{region}/ec2/physical/{values['id']}" for values in declared.values()}
    pairs = {pair.left.resource_id: pair for pair in result.matched}
    assert elements <= set(pairs)
    assert {str(pairs[resource_id].basis) for resource_id in elements} == {"physical_identity"}
    assert result.ambiguous == []
    # A declared field the observation lacks, or the reverse, is a reported difference.
    assert pairs[f"{account}/{region}/ec2/physical/nat-1"].differences

    # A template's logical IDs and a plan's unknown IDs never pair with an observation.
    for source in (
        CloudFormationRequest(
            path=FIXTURES_ROOT / "cloudformation" / "transit-gateway.yaml",
            account_id=account,
            region=region,
        ),
        plan_request(FIXTURES_ROOT / "terraform" / "transit-gateway.plan.json"),
    ):
        declaration = collect_snapshots(source, context=CONTEXT)[0]
        compared = compare_snapshots(observed, declaration, result_limit=1000, now=NOW)
        assert compared.matched == []
