"""Relative links and anchors in the README and docs must resolve."""

import re

import pytest

from tests.support.markdown import DOC_FILES, anchors, prose

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("document", DOC_FILES, ids=lambda path: path.name)
def test_relative_links_and_anchors_resolve(document):
    broken = []
    for target in _LINK.findall(prose(document)):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue
        path_part, _, fragment = target.partition("#")
        destination = (document.parent / path_part).resolve() if path_part else document
        if not destination.exists() or (
            fragment and destination.suffix == ".md" and fragment not in anchors(destination)
        ):
            broken.append(target)
    assert broken == []


def test_anchor_extraction_follows_github_slugs():
    from tests.paths import PROJECT_ROOT

    found = anchors(PROJECT_ROOT / "docs" / "comparison-results.md")
    assert {"matching-rules", "field-comparison", "qualification", "cli"} <= found
    assert "does-not-exist" not in found
