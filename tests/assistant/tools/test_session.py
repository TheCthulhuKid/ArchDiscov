from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.record import ReturnedIds
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.graph import GraphIndex
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

SNAPSHOTS = FIXTURES_ROOT / "snapshots"


def test_session_indexes_the_selected_snapshots_once_with_its_own_record() -> None:
    session = ToolSession.from_paths(SNAPSHOTS / "aws.json")
    assert isinstance(session.index, GraphIndex)
    assert session.index is session.index
    assert session.index.snapshot_id == load_snapshot(SNAPSHOTS / "aws.json").snapshot_id
    assert session.comparison_index is None
    assert session.record.returned() == ReturnedIds()
    compared = ToolSession.from_paths(
        SNAPSHOTS / "aws.json",
        comparison_path=SNAPSHOTS / "azure.json",
        limits=ToolLimits(result_limit=5),
    )
    assert compared.comparison_index is not None
    assert compared.comparison_index is compared.comparison_index
    assert compared.comparison_index.snapshot_id != compared.index.snapshot_id
    assert compared.limits.result_limit == 5
    assert compared.record is not session.record
