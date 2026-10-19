"""Build tagged snapshot variants from fixtures for ownership analysis tests."""

from archdiscov.models.enums import EvidenceValueStatus
from archdiscov.services import load_snapshot, upgrade_snapshot
from tests.paths import FIXTURES_ROOT

NO_TAGS = object()
TAG_EVIDENCE = "tags-test"
TAGS_POINTER = "/attributes/tags"


def walk(resources):
    for resource in resources:
        yield resource
        yield from walk(resource.children)


def all_resources(snapshot):
    for region in snapshot.regions.values():
        yield from walk(region.resources)
    yield from walk(getattr(snapshot, "global_resources", []))


def tagged(source, upgraded=False, *, tags=NO_TAGS, cite="present"):
    """Tag the first resource that already owns evidence, citing the tag map if asked.

    Returns the snapshot and the tagged resource's ID. The evidence record is cloned
    from one the resource already owns so its cloud context stays consistent.
    """
    if cite == "present" and tags is NO_TAGS:
        raise ValueError("a present tag citation needs tags")
    snapshot = load_snapshot(FIXTURES_ROOT / source)
    if upgraded:
        snapshot = upgrade_snapshot(snapshot)
    target = next(item for item in all_resources(snapshot) if item.evidence_ids)
    template = snapshot.evidence[target.evidence_ids[0]]
    attributes = {key: value for key, value in target.attributes.items() if key != "tags"}
    target.attributes = attributes if tags is NO_TAGS else {**attributes, "tags": tags}
    target.fact_evidence.pop(TAGS_POINTER, None)  # drop any fixture-provided citation
    if cite is not None:
        update = {"configuration_path": "Tags", "value_status": EvidenceValueStatus(cite)}
        if cite == "present":
            update["value"] = tags
        else:
            update.update(value=None, limitation="Tags were not collected.")
        snapshot.evidence[TAG_EVIDENCE] = template.model_copy(update=update)
        target.evidence_ids.append(TAG_EVIDENCE)
        target.fact_evidence[TAGS_POINTER] = [TAG_EVIDENCE]
    return snapshot, target.id
