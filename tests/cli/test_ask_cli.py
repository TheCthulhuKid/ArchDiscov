"""The `ask` command: run summary and answer in both forms, exit codes, and safe diagnostics.

Runs use the fake configurations of the ask service tests, so every outcome is scripted.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov import cli_ask
from archdiscov.assistant.answer_results import RenderedAnswer
from archdiscov.assistant.runs import RunTranscript
from archdiscov.cli import app
from archdiscov.cli_answer_output import ANSWER_JSON_VERSION
from archdiscov.cli_ask_output import ASK_JSON_VERSION
from archdiscov.services import AskCancelled, compare_snapshots, load_snapshot, save_comparison
from archdiscov.services.ask import RunSummary
from tests.assistant.test_answer_service import PLAIN_TEXT, SEARCH, _proposal, _rejected, _submit
from tests.assistant.test_orchestrator import _calls, _fail, _text
from tests.assistant.test_secret_leaks import SECRET
from tests.services.test_ask import (
    COMPARISON,
    QUESTION,
    SNAPSHOT,
    _cancelled,
    _fake,
    _session,
    _unadapted,
)

runner = CliRunner()
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


def _run(config: Path, *extra, question: str = QUESTION, snapshot: Path = SNAPSHOT):
    arguments = ["ask", question, "--snapshot", snapshot, "--config", config, *extra]
    result = runner.invoke(app, [str(item) for item in arguments])
    assert SECRET not in result.stdout + result.stderr
    return result


def _envelope(output: str) -> dict:
    data = json.loads(output)
    assert list(data) == ENVELOPE
    assert data["schema_version"] == ASK_JSON_VERSION
    assert RunSummary.model_validate(data["summary"]).model_dump(mode="json") == data["summary"]
    result = data["result"]
    assert list(result) == ["schema_version", "exit_code", "answer"]
    assert result["schema_version"] == ANSWER_JSON_VERSION
    RenderedAnswer.model_validate(result["answer"])
    return data


def _answered(tmp_path: Path) -> Path:
    return _fake(tmp_path, [_calls(SEARCH), _submit("a", _proposal(_session()))])


# ---- runs --------------------------------------------------------------------------


def test_an_answer_is_shown_after_its_run_summary(tmp_path: Path) -> None:
    result = _run(_answered(tmp_path))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[:2] == [f"Snapshot: {SNAPSHOT}", "Provider: fake, model fake"]
    assert lines[2].startswith("Limits: 8 provider requests, 32 tool calls (1 per turn)")
    assert lines[3:5] == ["Provider requests: 2", "Tool calls: search_resources 1"]
    assert lines[5].startswith("Tokens: ")
    assert lines[6:8] == ["", "Answer: answered, exit code 0"]
    assert "Status: insufficient_evidence" in lines and result.stderr == ""


def test_an_answer_is_a_json_envelope(tmp_path: Path) -> None:
    config = _answered(tmp_path)
    result = _run(config, "--json")
    assert result.exit_code == 0, result.output
    data = _envelope(result.stdout)
    assert (data["snapshot_path"], data["config_path"]) == (str(SNAPSHOT), str(config))
    assert data["comparison_path"] is None and data["transcript_path"] is None
    assert data["comparison_result_paths"] == []
    assert data["summary"]["usage"]["tool_calls"] == {"search_resources": 1}
    assert data["result"]["exit_code"] == 0
    assert data["result"]["answer"]["outcome"]["status"] == "answered"


@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_a_failure_is_explained_with_its_exit_code(tmp_path: Path, as_json: bool) -> None:
    turns = [_submit("a", _rejected(_session())), _submit("b", _rejected(_session()))]
    result = _run(_fake(tmp_path, turns), *(["--json"] if as_json else []))
    assert result.exit_code == 5 and result.stderr == ""
    if as_json:
        answer = _envelope(result.stdout)["result"]
        assert answer["exit_code"] == 5
        assert answer["answer"]["failure"]["reason"] == "rejected_after_repair"
    else:
        assert "Provider requests: 2 (repair requested)" in result.stdout
        assert "Answer: failed (rejected_after_repair), exit code 5" in result.stdout


@pytest.mark.parametrize(
    ("turns", "config", "code"),
    [
        (lambda: [_text(PLAIN_TEXT), _submit("a", _proposal(_session()))], "", 0),
        (lambda: [_text(PLAIN_TEXT), _text(PLAIN_TEXT)], "", 5),
        (lambda: [_fail("provider_unavailable")], "", 6),
        (lambda: [_calls(SEARCH)], "max_requests = 1\n", 6),
    ],
    ids=["repaired", "no-proposal", "provider-error", "limit"],
)
def test_the_command_exits_with_the_outcome_code(tmp_path: Path, turns, config, code) -> None:
    result = _run(_fake(tmp_path, turns(), config), "--json")
    assert result.exit_code == code
    assert _envelope(result.stdout)["result"]["exit_code"] == code


@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_a_cancelled_question_is_explained_and_exits_130(
    tmp_path: Path, monkeypatch, as_json: bool
) -> None:
    cancelled = _cancelled(tmp_path, monkeypatch)

    def interrupted(*args, **kwargs):
        raise AskCancelled(cancelled)

    monkeypatch.setattr(cli_ask, "ask_question", interrupted)
    result = _run(tmp_path / "provider.toml", *(["--json"] if as_json else []))
    assert result.exit_code == 130
    if as_json:
        assert _envelope(result.stdout)["result"]["exit_code"] == 130
    else:
        assert "Answer: failed (cancelled), exit code 130" in result.stdout


def test_a_comparison_and_a_transcript_are_reported(tmp_path: Path) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT), _text(PLAIN_TEXT)])
    transcript = tmp_path / "transcript.json"
    readable = _run(config, "--compare", COMPARISON, "--transcript", transcript)
    assert readable.exit_code == 5
    assert f"Comparison: {COMPARISON}" in readable.stdout
    assert f"Transcript: {transcript}" in readable.stdout
    RunTranscript.model_validate_json(transcript.read_text(encoding="utf-8"))
    again = tmp_path / "again.json"
    data = _envelope(_run(config, "--compare", COMPARISON, "--transcript", again, "--json").stdout)
    assert (data["comparison_path"], data["transcript_path"]) == (str(COMPARISON), str(again))
    assert [item["side"] for item in data["summary"]["snapshots"]] == ["primary", "comparison"]


# ---- errors before the run ---------------------------------------------------------


def _missing_config(tmp_path: Path) -> tuple[Path, list, dict]:
    return tmp_path / "missing.toml", [], {}


def _invalid_config(tmp_path: Path) -> tuple[Path, list, dict]:
    path = tmp_path / "provider.toml"
    path.write_text('kind = "fake"\n', encoding="utf-8")
    return path, [], {}


def _no_adapter(tmp_path: Path) -> tuple[Path, list, dict]:
    return _unadapted(tmp_path), [], {}


def _blank_question(tmp_path: Path) -> tuple[Path, list, dict]:
    return _answered(tmp_path), [], {"question": "   "}


def _unknown_snapshot(tmp_path: Path) -> tuple[Path, list, dict]:
    return _answered(tmp_path), [], {"snapshot": tmp_path / "missing.json"}


def _invalid_comparison(tmp_path: Path) -> tuple[Path, list, dict]:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version": "2"}', encoding="utf-8")
    return _answered(tmp_path), ["--compare", invalid], {}


def _missing_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    return _answered(tmp_path), ["--comparison-result", tmp_path / "missing.json"], {}


def _invalid_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    invalid = tmp_path / "comparison.json"
    invalid.write_text("{}", encoding="utf-8")
    return _answered(tmp_path), ["--comparison-result", invalid], {}


def _unselected_comparison_result(tmp_path: Path) -> tuple[Path, list, dict]:
    """A saved comparison of another snapshot with itself."""
    other = load_snapshot(COMPARISON)
    path = tmp_path / "comparison.json"
    save_comparison(compare_snapshots(other, other), path, left=other, right=other)
    return _answered(tmp_path), ["--comparison-result", path], {}


def _existing_transcript(tmp_path: Path) -> tuple[Path, list, dict]:
    existing = tmp_path / "transcript.json"
    existing.write_text("kept", encoding="utf-8")
    return _answered(tmp_path), ["--transcript", existing], {}


@pytest.mark.parametrize(
    ("case", "code", "error"),
    [
        (_missing_config, 1, "provider_config_unreadable"),
        (_invalid_config, 2, "provider_transcript_missing"),
        (_no_adapter, 2, "provider_adapter_unavailable"),
        (_blank_question, 2, "invalid_question"),
        (_unknown_snapshot, 1, "snapshot_io_error"),
        (_invalid_comparison, 2, "invalid_snapshot"),
        (_missing_comparison_result, 1, "snapshot_io_error"),
        (_invalid_comparison_result, 2, "invalid_comparison_result"),
        (_unselected_comparison_result, 2, "result_snapshot_unselected"),
        (_existing_transcript, 3, "snapshot_conflict"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)).strip("_"),
)
@pytest.mark.parametrize("as_json", [False, True], ids=["readable", "json"])
def test_errors_before_the_run_are_safe_diagnostics(
    tmp_path: Path, case, code: int, error: str, as_json: bool
) -> None:
    config, extra, overrides = case(tmp_path)
    result = _run(config, *extra, *(["--json"] if as_json else []), **overrides)
    assert result.exit_code == code
    if as_json:
        diagnostic = json.loads(result.stdout)["error"]
        assert diagnostic["code"] == error and result.stderr == ""
        if "--comparison-result" in extra:
            # A saved comparison's failure names that file, not the snapshot.
            assert diagnostic["path"] == str(extra[-1])
    else:
        assert result.stdout == ""
        assert result.stderr.startswith(f"Error [{error}]: ")
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["ask", "--snapshot", str(SNAPSHOT), "--config", "provider.toml"],
        ["ask", QUESTION, "--config", "provider.toml"],
        ["ask", QUESTION, "--snapshot", str(SNAPSHOT)],
    ],
    ids=["question", "snapshot", "config"],
)
def test_missing_arguments_are_usage_errors(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2 and "Missing" in result.stderr


def test_help_describes_the_command() -> None:
    result = runner.invoke(app, ["ask", "--help"])
    assert result.exit_code == 0
    for option in (
        "--snapshot",
        "--config",
        "--compare",
        "--comparison-result",
        "--transcript",
        "--json",
    ):
        assert option in result.stdout
    assert "no" in result.stdout and "conversation memory" in result.stdout
