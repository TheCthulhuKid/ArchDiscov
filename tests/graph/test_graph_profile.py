import itertools
import runpy

from archdiscov.models.resource_tree import walk_resources
from tests.paths import PROJECT_ROOT


def test_profile_selects_leaf_by_containment_not_identifier_order(monkeypatch):
    script = runpy.run_path(str(PROJECT_ROOT / "scripts" / "profile_graph_index.py"))
    build_snapshot, profile_case = script["synthetic_snapshot"], script["profile_case"]

    def reversed_tree(shape, size):
        """The script's tree with its containment reversed: the leaf has the first id."""
        snapshot = build_snapshot(shape, size)
        region = snapshot.regions[script["LOCATION"]]
        nodes = list(walk_resources(region.resources))
        for node in nodes:
            node.children = []
        nodes.reverse()
        if shape == "wide":
            nodes[0].children = nodes[1:]
        else:
            for parent, child in itertools.pairwise(nodes):
                parent.children = [child]
        region.resources = [nodes[0]]
        return snapshot

    monkeypatch.setitem(profile_case.__globals__, "synthetic_snapshot", reversed_tree)
    # The timings are the script's product, not the property under test.
    monkeypatch.setitem(profile_case.__globals__, "measure", lambda operation, calls, repeats: {})
    for shape in ("wide", "deep"):
        report = profile_case(shape, size=3, repeats=1, rounds=1)
        assert report["nodes"] == 3
        assert report["containment_edges"] == 2
        assert report["workloads"]["detached.resource_leaf"]["api_calls_per_workload"] == 1
