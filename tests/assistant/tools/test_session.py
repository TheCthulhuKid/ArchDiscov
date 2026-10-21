from pathlib import Path

import pytest

from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.graph import GraphIndex
from archdiscov.services.snapshots import SnapshotError, load_snapshot
from tests.paths import FIXTURES_ROOT

SNAPSHOTS = FIXTURES_ROOT / "snapshots"


def test_session_indexes_the_selected_snapshot_once() -> None:
    session = ToolSession.from_paths(SNAPSHOTS / "aws.json")
    assert isinstance(session.index, GraphIndex)
    assert session.index is session.index
    assert session.index.snapshot_id == load_snapshot(SNAPSHOTS / "aws.json").snapshot_id
    assert session.comparison_index is None
    assert session.limits == ToolLimits()
    assert session.record.returned() == ReturnedIds()


def test_session_can_hold_a_second_snapshot_for_comparisons() -> None:
    session = ToolSession.from_paths(
        SNAPSHOTS / "aws.json",
        comparison_path=SNAPSHOTS / "azure.json",
        limits=ToolLimits(result_limit=5),
    )
    assert session.comparison_index is not None
    assert session.comparison_index is session.comparison_index
    assert session.comparison_index.snapshot_id != session.index.snapshot_id
    assert session.limits.result_limit == 5


def test_session_accepts_loaded_snapshots_and_detaches_them() -> None:
    snapshot = load_snapshot(SNAPSHOTS / "aws.json")
    session = ToolSession(snapshot)
    before = session.index.resource_ids
    snapshot.regions.clear()
    snapshot.global_resources.clear()
    assert session.index.resource_ids == before


def test_each_session_has_its_own_record() -> None:
    first = ToolSession.from_paths(SNAPSHOTS / "aws.json")
    second = ToolSession.from_paths(SNAPSHOTS / "aws.json")
    assert first.record is not second.record


def test_invalid_snapshot_files_fail_when_the_user_opens_the_session(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError):
        ToolSession.from_paths(path)
