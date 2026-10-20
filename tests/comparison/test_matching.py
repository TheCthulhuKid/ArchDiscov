import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import ComparisonLimitationKind, MatchBasis
from archdiscov.services.comparison_matching import (
    AmbiguousIds,
    MatchedIds,
    iac_context,
    match_resources,
    pair_groups,
)
from tests.comparison.snapshots import (
    ACCOUNT,
    REGION,
    aws_resource,
    aws_snapshot,
    azure_snapshot,
    index,
)

PHYSICAL, LOGICAL = MatchBasis.PHYSICAL_IDENTITY, MatchBasis.LOGICAL_ADDRESS
VPC, SUBNET = "AWS::EC2::VPC", "AWS::EC2::Subnet"


def rid(name, region=REGION, kind="physical", account=ACCOUNT):
    return f"{account}/{region}/ec2/{kind}/{name}"


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


def test_matching_is_symmetric_apart_from_the_side_labels():
    left = index([aws_resource(physical="vpc-1"), aws_resource(physical="vpc-2")])
    right = index([aws_resource(physical="vpc-1"), aws_resource(physical="vpc-3")])
    forward, backward = match_resources(left, right), match_resources(right, left)
    assert [(m.right_id, m.left_id) for m in forward.matched] == [
        (m.left_id, m.right_id) for m in backward.matched
    ]
    assert forward.unmatched_left == backward.unmatched_right
    assert forward.unmatched_right == backward.unmatched_left


def test_the_same_physical_id_in_another_region_does_not_match():
    left = index([aws_resource(physical="vpc-1")])
    right = index([aws_resource(physical="vpc-1", region="us-east-1")], region="us-east-1")
    outcome = match_resources(left, right)
    assert outcome.matched == ()
    assert outcome.unmatched_left == left.resource_ids
    assert outcome.unmatched_right == right.resource_ids


def test_the_same_physical_id_with_another_native_type_does_not_match():
    left = index([aws_resource(SUBNET, physical="x-1")])
    right = index([aws_resource(VPC, physical="x-1")])
    outcome = match_resources(left, right)
    assert outcome.matched == ()
    assert len(outcome.unmatched_left) == len(outcome.unmatched_right) == 1


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


def test_aws_matching_is_case_sensitive():
    left = index([aws_resource(physical="vpc-AAA")])
    right = index([aws_resource(physical="vpc-aaa")])
    assert match_resources(left, right).matched == ()


# ---- logical addresses --------------------------------------------------------------


def test_logical_addresses_match_inside_one_iac_context_including_module_paths():
    left = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_state")
    right = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_plan")
    [matched] = match_resources(left, right).matched
    assert matched.basis == LOGICAL


@pytest.mark.parametrize(
    "right_address",
    ["module.other.aws_vpc.main", "aws_vpc.main", 'module.network.aws_vpc.main["a"]'],
)
def test_a_different_module_path_or_instance_is_not_the_same_address(right_address):
    left = index([aws_resource(logical="module.network.aws_vpc.main")], "terraform_state")
    right = index([aws_resource(logical=right_address)], "terraform_plan")
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


def test_iac_contexts_are_per_source_family():
    from archdiscov.models.enums import SnapshotSourceKind as K

    assert iac_context(K.TERRAFORM_STATE) == iac_context(K.TERRAFORM_PLAN) == "terraform"
    assert iac_context(K.CLOUDFORMATION) == "cloudformation"
    assert {iac_context(K.AWS), iac_context(K.MOCK), iac_context(K.AZURE_FIXTURE)} == {None}


# ---- ambiguity ----------------------------------------------------------------------


def key(identifier):
    return (PHYSICAL, "AWS::EC2::VPC", identifier, "")


def test_duplicate_keys_are_ambiguous_with_every_candidate_listed_and_none_chosen():
    left = {key("a"): ("AWS::EC2::VPC a", ["l1", "l2"]), key("b"): ("b", ["l3"])}
    right = {key("a"): ("AWS::EC2::VPC a", ["r1"]), key("b"): ("b", ["r2", "r3"])}
    matched, ambiguous = pair_groups(left, right)
    assert matched == []
    assert ambiguous == [
        AmbiguousIds(PHYSICAL, "AWS::EC2::VPC a", ("l1", "l2"), ("r1",)),
        AmbiguousIds(PHYSICAL, "b", ("l3",), ("r2", "r3")),
    ]


def test_a_duplicate_with_no_counterpart_pairs_nothing_and_unique_keys_still_match():
    left = {key("a"): ("a", ["l1", "l2"]), key("c"): ("c", ["l4"])}
    right = {key("b"): ("b", ["r1"]), key("c"): ("c", ["r4"])}
    matched, ambiguous = pair_groups(left, right)
    assert matched == [MatchedIds("l4", "r4", PHYSICAL)]
    assert ambiguous == []


# ---- determinism --------------------------------------------------------------------


def test_the_outcome_is_deterministic_and_covers_every_resource_once():
    resources = [aws_resource(physical=f"vpc-{n}") for n in (3, 1, 2)]
    left = index(resources)
    right = index(list(reversed(resources[:2])))
    first, second = match_resources(left, right), match_resources(left, right)
    assert first == second
    left_seen = [m.left_id for m in first.matched] + list(first.unmatched_left)
    assert sorted(left_seen) == list(left.resource_ids)
    assert [m.left_id for m in first.matched] == sorted(m.left_id for m in first.matched)


def test_snapshot_sources_are_exposed_through_the_index():
    for source in ("aws", "mock", "terraform_state", "terraform_plan", "cloudformation"):
        resource = (
            aws_resource(physical="vpc-1", logical="x")
            if source != "aws"
            else aws_resource(physical="vpc-1")
        )
        assert index([resource], source).source_kind == source
    assert GraphIndex(azure_snapshot()).source_kind == "azure_fixture"
    assert aws_snapshot([aws_resource(physical="v")]).source.kind == "aws"


def test_recorded_fixtures_match_only_inside_their_own_context():
    from archdiscov.services import load_snapshot
    from tests.paths import FIXTURES_ROOT

    def load(name):
        return GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / f"{name}.json"))

    plan, template, observed = load("terraform-plan"), load("cloudformation"), load("aws")
    assert match_resources(plan, template).matched == ()
    assert match_resources(plan, observed).matched == ()
    [self_match] = match_resources(plan, plan).matched
    assert (
        self_match.basis == LOGICAL
        and "module.network" in plan.get_resource(self_match.left_id).identity.logical_address
    )
    [same] = match_resources(observed, observed).matched[:1]
    assert same.basis == PHYSICAL
