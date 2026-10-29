"""Relative links and anchors in the README and docs must resolve."""

import re

from tests.support.markdown import DOC_FILES, anchors, prose

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def _broken_links(document):
    """The relative links and anchors in `document` that do not resolve."""
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
    return broken


def test_relative_links_and_anchors_resolve_in_every_document():
    broken = {document.name: _broken_links(document) for document in DOC_FILES}
    assert {name: targets for name, targets in broken.items() if targets} == {}


def test_anchor_extraction_follows_github_slugs():
    from tests.paths import PROJECT_ROOT

    found = anchors(PROJECT_ROOT / "docs" / "comparison-results.md")
    assert {"matching-rules", "field-comparison", "qualification", "cli"} <= found
    assert "does-not-exist" not in found
