from archdiscov.graph import GraphIndex
from archdiscov.models.enums import ComparisonLimitationKind, MatchBasis
from archdiscov.services.comparison_matching import (
    AmbiguousIds,
    MatchedIds,
    match_resources,
    pair_groups,
)
from tests.support.comparison_data import aws_resource, azure_snapshot, index, rid

PHYSICAL, LOGICAL = MatchBasis.PHYSICAL_IDENTITY, MatchBasis.LOGICAL_ADDRESS
SUBNET = "AWS::EC2::Subnet"


def ids(outcome):
    return (
        [(m.left_id, m.right_id, m.basis) for m in outcome.matched],
        list(outcome.unmatched_left),
        list(outcome.unmatched_right),
        [(a.basis, a.key) for a in outcome.ambiguous],
    )


# ---- physical identity --------------------------------------------------------------


def test_equal_physical_identities_match_and_the_rest_are_unmatched_on_their_side():
    left = index([aws_resource(physical="vpc-1"), aws_resource(physical="vpc-2")])
    right = index([aws_resource(physical="vpc-1"), aws_resource(physical="vpc-3")])
    matched, unmatched_left, unmatched_right, ambiguous = ids(match_resources(left, right))
    assert matched == [(rid("vpc-1"), rid("vpc-1"), PHYSICAL)]
    assert unmatched_left == [rid("vpc-2")]
    assert unmatched_right == [rid("vpc-3")]
    assert ambiguous == []
    # Matching is symmetric apart from the side labels.
    forward, backward = match_resources(left, right), match_resources(right, left)
    assert [(m.right_id, m.left_id) for m in forward.matched] == [
        (m.left_id, m.right_id) for m in backward.matched
    ]
    assert forward.unmatched_left == backward.unmatched_right
    assert forward.unmatched_right == backward.unmatched_left


def test_the_same_physical_id_in_another_region_type_or_case_does_not_match():
    left = index([aws_resource(physical="vpc-1")])
    elsewhere = index([aws_resource(physical="vpc-1", region="us-east-1")], region="us-east-1")
    outcome = match_resources(left, elsewhere)
    assert outcome.matched == ()
    assert outcome.unmatched_left == left.resource_ids
    assert outcome.unmatched_right == elsewhere.resource_ids
    assert match_resources(index([aws_resource(SUBNET, physical="vpc-1")]), left).matched == ()
    # AWS identifiers are case-sensitive, unlike Azure ARM IDs.
    assert match_resources(index([aws_resource(physical="VPC-1")]), left).matched == ()


def test_names_and_short_ids_never_match():
    left = index([aws_resource(physical="vpc-1", name="main")])
    right = index([aws_resource(physical="vpc-2", name="main")])
    outcome = match_resources(left, right)
    assert outcome.matched == ()
    assert outcome.unmatched_left and outcome.unmatched_right


def test_resources_of_different_accounts_or_clouds_match_nothing():
    left = index([aws_resource(physical="vpc-1")])
    other_account = index(
        [aws_resource(physical="vpc-1", account="999999999999")], account="999999999999"
    )
    outcome = match_resources(left, other_account)
    assert outcome.matched == () and outcome.limitations == (
        ComparisonLimitationKind.SCOPE_MISMATCH,
    )
    assert outcome.unmatched_left == left.resource_ids
    assert outcome.unmatched_right == other_account.resource_ids
    cross = match_resources(left, GraphIndex(azure_snapshot()))
    assert cross.matched == () and cross.limitations == (ComparisonLimitationKind.CROSS_CLOUD,)
    assert len(cross.unmatched_right) == 3 and cross.unmatched_left == left.resource_ids


# ---- Azure --------------------------------------------------------------------------


def test_azure_arm_ids_match_case_insensitively():
    left, right = GraphIndex(azure_snapshot()), GraphIndex(azure_snapshot(upper=True))
    outcome = match_resources(left, right)
    assert len(outcome.matched) == 3
    assert {m.basis for m in outcome.matched} == {PHYSICAL}
    assert outcome.unmatched_left == outcome.unmatched_right == ()


# ---- logical addresses --------------------------------------------------------------


def test_logical_addresses_match_inside_one_iac_context_including_module_paths():
    left = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_state")
    right = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_plan")
    [matched] = match_resources(left, right).matched
    assert matched.basis == LOGICAL


def test_a_different_module_path_is_not_the_same_address():
    left = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_state")
    right = index([aws_resource(logical="module.other.aws_vpc.main")], "terraform_plan")
    outcome = match_resources(left, right)
    assert outcome.matched == ()
    assert outcome.unmatched_left and outcome.unmatched_right


def test_logical_addresses_never_match_across_iac_contexts_or_observations():
    address = "network/MainVpc"
    template = index([aws_resource(logical=address)], "cloudformation")
    terraform = index([aws_resource(logical=address)], "terraform_plan")
    assert match_resources(template, terraform).matched == ()
    observed = index([aws_resource(logical=address)], "aws", seed=2)
    assert match_resources(observed, observed).matched == ()  # no IaC context at all
    assert match_resources(template, template).matched != ()


def test_a_physical_identity_never_falls_back_to_its_logical_address():
    state = index([aws_resource(physical="vpc-1", logical="aws_vpc.main")], "terraform_state")
    plan = index([aws_resource(logical="aws_vpc.main")], "terraform_plan")
    outcome = match_resources(state, plan)
    assert outcome.matched == ()
    assert outcome.unmatched_left == state.resource_ids
    assert outcome.unmatched_right == plan.resource_ids


def test_observed_resources_match_terraform_state_by_physical_identity():
    observed = index([aws_resource(physical="vpc-1")], "aws")
    state = index([aws_resource(physical="vpc-1", logical="aws_vpc.main")], "terraform_state")
    [matched] = match_resources(observed, state).matched
    assert matched.basis == PHYSICAL


# ---- ambiguity ----------------------------------------------------------------------


def key(identifier):
    return (PHYSICAL, "AWS::EC2::VPC", identifier, "")


def test_duplicate_keys_are_ambiguous_with_every_candidate_listed_and_none_chosen():
    left = {
        key("a"): ("AWS::EC2::VPC a", ["l1", "l2"]),
        key("b"): ("b", ["l3"]),
        key("c"): ("c", ["l4"]),
        key("d"): ("d", ["l5", "l6"]),
    }
    right = {
        key("a"): ("AWS::EC2::VPC a", ["r1"]),
        key("b"): ("b", ["r2", "r3"]),
        key("c"): ("c", ["r4"]),
    }
    matched, ambiguous = pair_groups(left, right)
    # Unique keys still match; a duplicate without a counterpart pairs nothing.
    assert matched == [MatchedIds("l4", "r4", PHYSICAL)]
    assert ambiguous == [
        AmbiguousIds(PHYSICAL, "AWS::EC2::VPC a", ("l1", "l2"), ("r1",)),
        AmbiguousIds(PHYSICAL, "b", ("l3",), ("r2", "r3")),
    ]
