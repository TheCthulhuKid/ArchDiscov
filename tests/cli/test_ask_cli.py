"""The `ask` command: run summary and answer in both forms, exit codes, and safe diagnostics.

Runs use the fake configurations of the ask service tests, so every outcome is scripted.
"""

import json
from pathlib import Path

from click import unstyle
from typer.testing import CliRunner

from archdiscov import cli_ask
from archdiscov.assistant.answer_output import ANSWER_JSON_VERSION
from archdiscov.assistant.answer_results import RenderedAnswer
from archdiscov.assistant.ask_results import RunSummary
from archdiscov.assistant.runs import RunTranscript
from archdiscov.cli import app
from archdiscov.cli_ask_output import ASK_JSON_VERSION
from archdiscov.services import AskCancelled, compare_snapshots, load_snapshot, save_comparison
from tests.support.ask_cli_cases import ENVELOPE, PRE_RUN_ERRORS, answered_config
from tests.support.ask_runs import (
    ASK_QUESTION,
    COMPARISON,
    SNAPSHOT,
    cancelled_ask,
    fake_config,
    network_session,
)
from tests.support.scripted_answers import PLAIN_TEXT, rejected_proposal, submit_turn
from tests.support.scripted_turns import fail_turn, text_turn
from tests.support.secrets import SECRET

runner = CliRunner()


def _run(config: Path, *extra):
    arguments = ["ask", ASK_QUESTION, "--snapshot", SNAPSHOT, "--config", config, *extra]
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


# ---- runs --------------------------------------------------------------------------


def test_an_answer_is_shown_after_its_run_summary(tmp_path: Path) -> None:
    result = _run(answered_config(tmp_path))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[:2] == [f"Snapshot: {SNAPSHOT}", "Provider: fake, model fake"]
    assert lines[2].startswith("Limits: 8 provider requests, 32 tool calls (1 per turn)")
    assert lines[3:5] == ["Provider requests: 2", "Tool calls: search_resources 1"]
    assert lines[5].startswith("Tokens: ")
    assert lines[6:8] == ["", "Answer: answered, exit code 0"]
    assert "Status: insufficient_evidence" in lines and result.stderr == ""


def test_an_answer_is_a_json_envelope(tmp_path: Path) -> None:
    config = answered_config(tmp_path)
    result = _run(config, "--json")
    assert result.exit_code == 0, result.output
    data = _envelope(result.stdout)
    assert (data["snapshot_path"], data["config_path"]) == (str(SNAPSHOT), str(config))
    assert data["comparison_path"] is None and data["transcript_path"] is None
    assert data["comparison_result_paths"] == []
    assert data["summary"]["usage"]["tool_calls"] == {"search_resources": 1}
    assert data["result"]["exit_code"] == 0
    assert data["result"]["answer"]["outcome"]["status"] == "answered"


def test_a_failure_is_explained_with_its_exit_code(tmp_path: Path) -> None:
    turns = [
        submit_turn("a", rejected_proposal(network_session())),
        submit_turn("b", rejected_proposal(network_session())),
    ]
    result = _run(fake_config(tmp_path, turns))
    assert result.exit_code == 5 and result.stderr == ""
    assert "Provider requests: 2 (repair requested)" in result.stdout
    assert "Answer: failed (rejected_after_repair), exit code 5" in result.stdout


def test_the_command_exits_with_the_outcome_code(tmp_path: Path) -> None:
    result = _run(fake_config(tmp_path, [fail_turn("provider_unavailable")]), "--json")
    assert result.exit_code == 6
    answer = _envelope(result.stdout)["result"]
    assert answer["exit_code"] == 6
    assert answer["answer"]["failure"]["reason"] == "provider_error"


def test_a_cancelled_question_is_explained_and_exits_130(tmp_path: Path, monkeypatch) -> None:
    cancelled = cancelled_ask(tmp_path, monkeypatch)

    def interrupted(*args, **kwargs):
        raise AskCancelled(cancelled)

    monkeypatch.setattr(cli_ask, "ask_question", interrupted)
    result = _run(tmp_path / "provider.toml")
    assert result.exit_code == 130
    assert "Answer: failed (cancelled), exit code 130" in result.stdout


def test_a_comparison_and_a_transcript_are_reported(tmp_path: Path) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT), text_turn(PLAIN_TEXT)])
    left, right = load_snapshot(SNAPSHOT), load_snapshot(COMPARISON)
    result = tmp_path / "comparison.json"
    save_comparison(compare_snapshots(left, right), result, left=left, right=right)
    selection = ["--compare", COMPARISON, "--comparison-result", result]
    transcript = tmp_path / "transcript.json"
    readable = _run(config, *selection, "--transcript", transcript)
    assert readable.exit_code == 5
    assert f"Comparison: {COMPARISON}" in readable.stdout
    assert f"Comparison result: {result}" in readable.stdout
    assert f"Transcript: {transcript}" in readable.stdout
    RunTranscript.model_validate_json(transcript.read_text(encoding="utf-8"))
    again = tmp_path / "again.json"
    data = _envelope(_run(config, *selection, "--transcript", again, "--json").stdout)
    assert (data["comparison_path"], data["transcript_path"]) == (str(COMPARISON), str(again))
    assert data["comparison_result_paths"] == [str(result)]
    assert [item["side"] for item in data["summary"]["snapshots"]] == ["primary", "comparison"]


# ---- errors before the run ---------------------------------------------------------


def test_errors_before_the_run_are_safe_diagnostics(tmp_path: Path) -> None:
    # One error through the command; the ask service tests reach the others through the same
    # diagnostics.
    [(case, code, error)] = [
        item for item in PRE_RUN_ERRORS if item[2] == "result_snapshot_unselected"
    ]
    config, extra, _ = case(tmp_path)
    readable, result = (_run(config, *extra, *flags) for flags in ([], ["--json"]))
    assert (readable.exit_code, result.exit_code) == (code, code)
    assert readable.stdout == ""
    assert readable.stderr.startswith(f"Error [{error}]: ")
    diagnostic = json.loads(result.stdout)["error"]
    assert diagnostic["code"] == error and result.stderr == ""
    # A saved comparison's failure names that file, not the snapshot.
    assert diagnostic["path"] == str(extra[-1])


def test_help_describes_the_command(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("TTY_COMPATIBLE", "0")
    result = runner.invoke(app, ["ask", "--help"])
    assert result.exit_code == 0
    text = unstyle(result.stdout)
    for option in (
        "--snapshot",
        "--config",
        "--compare",
        "--comparison-result",
        "--transcript",
        "--json",
    ):
        assert option in text
    assert "no" in text and "conversation memory" in text
