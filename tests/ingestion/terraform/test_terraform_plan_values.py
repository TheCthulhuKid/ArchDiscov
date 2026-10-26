import json
import random
from itertools import count

from archdiscov.ingestion.terraform.plan_values import sanitize_plan_values


def _masked_tree(rng: random.Random, depth: int, labels: count):
    """A value with matching sensitive and unknown masks. Each leaf is tagged with the mask
    that covers it, so the test can tell whether a sensitive or unknown leaf survived."""
    kind = rng.choice(["leaf", "leaf", "object", "list"]) if depth < 4 else "leaf"
    if kind == "leaf":
        mark = rng.choice(["sensitive", "unknown", "plain", "plain"])
        return f"{mark}-{next(labels)}", mark == "sensitive", mark == "unknown"
    children = [_masked_tree(rng, depth + 1, labels) for _ in range(rng.randint(0, 3))]
    values, sensitive, unknown = ([child[part] for child in children] for part in range(3))
    if kind == "object":
        names = [f"k{index}" for index in range(len(children))]
        values, sensitive, unknown = (
            dict(zip(names, part, strict=True)) for part in (values, sensitive, unknown)
        )
    # A whole sensitive container hides every descendant, sensitive-tagged or not.
    if rng.random() < 0.15:
        values = json.loads(json.dumps(values).replace("plain-", "sensitive-"))
        sensitive = True
    return values, sensitive, unknown


def test_no_sensitive_or_unknown_value_survives():
    for seed in range(50):
        rng = random.Random(seed)
        labels = count()
        trees = [_masked_tree(rng, 1, labels) for _ in range(rng.randint(1, 4))]
        values, sensitive, unknown = (
            {f"a{index}": tree[part] for index, tree in enumerate(trees)} for part in range(3)
        )

        safe, redacted, unknown_paths = sanitize_plan_values(values, sensitive, unknown)

        rendered = json.dumps(safe)
        assert "sensitive-" not in rendered, seed
        assert "unknown-" not in rendered, seed
        assert all(path.startswith("/a") for path in [*redacted, *unknown_paths]), seed
