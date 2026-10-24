"""The `ask --json` envelope keys, and one `ask` invocation per error raised before the run."""

from pathlib import Path

from archdiscov.services import compare_snapshots, load_snapshot, save_comparison
from tests.support.ask_runs import COMPARISON, fake_config, network_session
from tests.support.provider_data import missing_profile_config
from tests.support.scripted_answers import SEARCH, answer_proposal, submit_turn
from tests.support.scripted_turns import calls_turn

ENVELOPE = [
    "schema_version",
    "snapshot_path",
    "comparison_path",
    "comparison_result_paths",
    "config_path",
    "transcript_path",
    "summary",
    "result",
]


def answered_config(tmp_path: Path) -> Path:
    """A fake configuration whose run searches, then submits an accepted answer."""
    return fake_config(
        tmp_path, [calls_turn(SEARCH), submit_turn("a", answer_proposal(network_session()))]
    )


def _missing_config(tmp_path: Path) -> tuple[Path, list, dict]:
    return tmp_path / "missing.toml", [], {}


def _invalid_config(tmp_path: Path) -> tuple[Path, list, dict]:
    path = tmp_path / "provider.toml"
    path.write_text('kind = "fake"\n', encoding="utf-8")
    return path, [], {}


def _missing_profile(tmp_path: Path) -> tuple[Path, list, dict]:
    return missing_profile_config(tmp_path), [], {}


def _blank_question(tmp_path: Path) -> tuple[Path, list, dict]:
    return answered_config(tmp_path), [], {"question": "   "}


def _unknown_snapshot(tmp_path: Path) -> tuple[Path, list, dict]:
    return answered_config(tmp_path), [], {"snapshot": tmp_path / "missing.json"}


def _invalid_comparison(tmp_path: Path) -> tuple[Path, list, dict]:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version": "2"}', encoding="utf-8")
    return answered_config(tmp_path), ["--compare", invalid], {}


def _missing_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    return answered_config(tmp_path), ["--comparison-result", tmp_path / "missing.json"], {}


def _invalid_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    invalid = tmp_path / "comparison.json"
    invalid.write_text("{}", encoding="utf-8")
    return answered_config(tmp_path), ["--comparison-result", invalid], {}


def _unselected_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    """A saved comparison of another snapshot with itself."""
    other = load_snapshot(COMPARISON)
    path = tmp_path / "comparison.json"
    save_comparison(compare_snapshots(other, other), path, left=other, right=other)
    return answered_config(tmp_path), ["--comparison-result", path], {}


def _existing_transcript(tmp_path: Path) -> tuple[Path, list, dict]:
    existing = tmp_path / "transcript.json"
    existing.write_text("kept", encoding="utf-8")
    return answered_config(tmp_path), ["--transcript", existing], {}


# Each case's exit code and diagnostic code; the command documentation lists them too.
PRE_RUN_ERRORS = [
    (_missing_config, 1, "provider_config_unreadable"),
    (_invalid_config, 2, "provider_transcript_missing"),
    (_missing_profile, 2, "provider_profile_unavailable"),
    (_blank_question, 2, "invalid_question"),
    (_unknown_snapshot, 1, "snapshot_io_error"),
    (_invalid_comparison, 2, "invalid_snapshot"),
    (_missing_comparison_result, 1, "snapshot_io_error"),
    (_invalid_comparison_result, 2, "invalid_comparison_result"),
    (_unselected_comparison_result, 2, "result_snapshot_unselected"),
    (_existing_transcript, 3, "snapshot_conflict"),
]
