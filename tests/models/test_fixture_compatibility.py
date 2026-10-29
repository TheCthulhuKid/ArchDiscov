"""Guard: models gain optional fields without changing how existing files validate."""

import pytest

from archdiscov.models.canonical import sha256_json
from archdiscov.services.analyses import load_analysis
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

# Canonical-JSON hashes of each fixture's validated dump. A change here means an existing
# snapshot or saved result no longer validates to the same data.
SNAPSHOT_HASHES = {
    "aws.json": "28df67155c04e01d85f452a5d400ebe4d03d912e03170255114918e2c37c2518",
    "azure.json": "b3f074fff69a17f95482d26f67d86cd0a7b9d34948025869685bdfe2bd939121",
    "cloudformation.json": "1aa32bd426a2ccbcf2c5682149e32977d4b59bdc0530d0754e91ce263afd2047",
    "dependency-targets.json": "69dba54942094e204de628da7bca393e2ee312e4344bc6501f164d53667354fa",
    "hierarchy.json": "e391676f62b651a5a738fa996bbf649d3368b6df2764de34e703fd2d7b7e27a5",
    "iam.json": "d07c734afe73922c2696ce6d1ee10cba874d46c168ab4a47710936bea21032cc",
    "load-balancing.json": "5927f7b716b481a4dafe3872f246ad071e3f31969e89888f1a9d9fbca07f8364",
    "network.json": "97f397287b876bdb15830640e4956151e0e6bd5c41aa5a48903a8278c2aa9dc9",
    "partial-coverage.json": "7186bf6b23f30dc183bccd432310e5f54aee0a12276d4c579a05d1c34e05eb93",
    "terraform-plan.json": "f6970ea2b8dcd35b3caf2d84a44ae42d59a2ab9ebf8be20aa90f07da17df59d2",
}
RESULT_HASHES = {
    "ownership.json": "589131714ebd2d9021e228fead8c8a0715e3a7f12a5f4b5700b8346001a7e442",
    "partial-path.json": "78874319628a6d9c617f9929020757bde6aa2e8eef5b1f1a38e1ab11e4c02f36",
}
# Fixtures added by phase 6 are checked by their own tests, not pinned here.
PHASE_SIX_SNAPSHOTS = frozenset(
    {
        "dual-stack.json",
        "egress-gateways.json",
        "endpoints-and-prefix-lists.json",
        "transit-gateway.json",
        "vpc-peering.json",
    }
)


def test_every_existing_snapshot_fixture_is_pinned() -> None:
    names = {path.name for path in (FIXTURES_ROOT / "snapshots").glob("*.json")}
    assert names - PHASE_SIX_SNAPSHOTS == set(SNAPSHOT_HASHES)


@pytest.mark.parametrize(("name", "digest"), SNAPSHOT_HASHES.items())
def test_snapshot_validates_to_the_same_data(name: str, digest: str) -> None:
    snapshot = load_snapshot(FIXTURES_ROOT / "snapshots" / name)
    assert sha256_json(snapshot.model_dump(mode="json")) == digest


@pytest.mark.parametrize(("name", "digest"), RESULT_HASHES.items())
def test_saved_result_validates_to_the_same_data(name: str, digest: str) -> None:
    result = load_analysis(FIXTURES_ROOT / "analysis" / name)
    assert sha256_json(result.model_dump(mode="json")) == digest
