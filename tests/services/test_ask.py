"""The ask service: configuration to provider, snapshots to session, and a summarised answer.

Runs use fake configurations written next to their transcripts; errors before the run go
through the CLI diagnostics so their safe output and exit codes are checked as users see them.
"""

import asyncio
import json
import os
import signal
from pathlib import Path

import pytest
import typer

from archdiscov.assistant.answer_results import FAILURE_EXIT_CODES, FailureReason
from archdiscov.assistant.conversation import MAX_TEXT_CHARS
from archdiscov.assistant.runs import RunLimits, RunTranscript
from archdiscov.cli_diagnostics import safe_assistant_failures
from archdiscov.services import ask as ask_module
from archdiscov.services.ask import AskCancelled, AskResult, ToolUsage, ask_question
from archdiscov.services.snapshot_errors import SnapshotIOError
from tests.support.ask_runs import (
    ASK_QUESTION,
    COMPARISON,
    SNAPSHOT,
    cancelled_ask,
    fake_config,
    network_session,
)
from tests.support.provider_data import MISSING_PROFILE, missing_profile_config
from tests.support.scripted_answers import (
    PLAIN_TEXT,
    SEARCH,
    answer_proposal,
    rejected_proposal,
    submit_turn,
)
from tests.support.scripted_turns import HangingProvider, calls_turn, fail_turn, text_turn
from tests.support.secrets import SECRET


def _ask(config_path: Path, **paths: Path) -> AskResult:
    return ask_question(
        ASK_QUESTION,
        snapshot_path=paths.get("snapshot_path", SNAPSHOT),
        config_path=config_path,
        comparison_path=paths.get("comparison_path"),
        transcript_path=paths.get("transcript_path"),
    )


def _check_summary(result: AskResult) -> None:
    summary, transcript = result.summary, result.answer.transcript
    assert summary.usage == ToolUsage.from_transcript(transcript)
    assert summary.usage.provider_requests == len(transcript.turns)
    assert summary.snapshots == result.answer.rendered.snapshots
    assert (summary.provider_kind, summary.model) == (transcript.provider_kind, transcript.model)
    assert summary.limits == transcript.limits
    assert AskResult.model_validate_json(result.model_dump_json()) == result
    assert SECRET not in result.model_dump_json()


# ---- runs --------------------------------------------------------------------------


def test_an_answer_is_summarised(tmp_path: Path) -> None:
    config = fake_config(
        tmp_path, [calls_turn(SEARCH), submit_turn("a", answer_proposal(network_session()))]
    )
    result = _ask(config)
    assert result.answer.outcome.status == "answered" and result.answer.outcome.exit_code == 0
    usage = result.summary.usage
    assert usage.provider_requests == 2 and usage.tool_calls == {"search_resources": 1}
    assert not usage.repair_requested
    assert result.summary.limits == RunLimits()
    _check_summary(result)


def test_a_repaired_answer_records_the_repair(tmp_path: Path) -> None:
    config = fake_config(
        tmp_path, [text_turn(PLAIN_TEXT), submit_turn("a", answer_proposal(network_session()))]
    )
    result = _ask(config)
    assert result.answer.outcome.status == "answered_after_repair"
    assert result.summary.usage.repair_requested
    _check_summary(result)


@pytest.mark.parametrize(
    ("turns", "config", "reason"),
    [
        (
            lambda: [
                submit_turn("a", rejected_proposal(network_session())),
                submit_turn("b", rejected_proposal(network_session())),
            ],
            "",
            FailureReason.REJECTED_AFTER_REPAIR,
        ),
        (lambda: [fail_turn("provider_unavailable")], "", FailureReason.PROVIDER_ERROR),
        (lambda: [calls_turn(SEARCH)], "max_requests = 1\n", FailureReason.LIMIT_REACHED),
    ],
    ids=["exit-5", "exit-6-provider", "exit-6-limit"],
)
def test_failures_are_outcomes(tmp_path: Path, turns, config: str, reason: FailureReason) -> None:
    config_path = fake_config(tmp_path, turns(), config)
    result = _ask(config_path)
    outcome = result.answer.outcome
    assert outcome.failure == reason and outcome.exit_code == FAILURE_EXIT_CODES[reason]
    assert result.answer.rendered.failure.reason == reason
    _check_summary(result)


def test_configured_limits_bound_the_run(tmp_path: Path) -> None:
    config = fake_config(tmp_path, [calls_turn(SEARCH)], "max_requests = 1\nmax_tool_calls = 4\n")
    limits = _ask(config).summary.limits
    assert (limits.max_provider_requests, limits.max_tool_calls) == (1, 4)


def test_a_comparison_snapshot_is_summarised_as_the_second_side(tmp_path: Path) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT), text_turn(PLAIN_TEXT)])
    result = _ask(config, comparison_path=COMPARISON)
    assert [item.side for item in result.summary.snapshots] == ["primary", "comparison"]
    _check_summary(result)


class _InterruptedProvider(HangingProvider):
    """Interrupts the process (as Ctrl-C does) while its request is in progress."""

    async def complete_async(self, request):
        asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGINT)
        return await super().complete_async(request)


def test_an_interrupt_raises_the_explained_cancellation(tmp_path: Path, monkeypatch) -> None:
    config_path = fake_config(tmp_path, [text_turn()])
    provider = _InterruptedProvider()
    built = ask_module.load_provider
    monkeypatch.setattr(
        ask_module, "load_provider", lambda path, *, environ=None: (built(path)[0], provider)
    )
    with pytest.raises(AskCancelled) as raised:
        _ask(config_path)
    result = raised.value.result
    assert result.answer.outcome.exit_code == 130 and provider.settled
    _check_summary(result)


def test_cancelling_explains_the_cancelled_question(tmp_path: Path, monkeypatch) -> None:
    result = cancelled_ask(tmp_path, monkeypatch)
    assert result.answer.outcome.failure == "cancelled"
    assert result.answer.outcome.exit_code == 130
    _check_summary(result)


# ---- errors before the run ---------------------------------------------------------


def _failure(capsys, action) -> tuple[int, dict]:
    with pytest.raises(typer.Exit) as raised, safe_assistant_failures(SNAPSHOT, as_json=True):
        action()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    return raised.value.exit_code, json.loads(output.out)["error"]


def test_an_unsendable_question_is_refused_first(tmp_path: Path, capsys) -> None:
    question = "q" * (MAX_TEXT_CHARS + 1)
    missing = tmp_path / "missing.toml"
    code, error = _failure(
        capsys, lambda: ask_question(question, snapshot_path=SNAPSHOT, config_path=missing)
    )
    assert (code, error["code"]) == (2, "invalid_question")
    # A fixed message cannot echo the question.
    assert error["message"] == str(ask_module.InvalidQuestionError())
    assert str(MAX_TEXT_CHARS) in error["message"]


def test_a_missing_configuration_is_unreadable(tmp_path: Path, capsys) -> None:
    code, error = _failure(capsys, lambda: _ask(tmp_path / "missing.toml"))
    assert (code, error["code"]) == (1, "provider_config_unreadable")
    assert error["path"] == str(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ('kind = "fake"\n', "provider_transcript_missing"),
        ('kind = "fake"\nmax_requests = "8"\n', "provider_config_invalid"),
        ("kind = ", "provider_config_malformed"),
    ],
    ids=["no-transcript", "invalid", "malformed"],
)
def test_invalid_configuration_is_invalid_input(tmp_path: Path, capsys, config, expected) -> None:
    path = tmp_path / "provider.toml"
    path.write_text(config, encoding="utf-8")
    code, error = _failure(capsys, lambda: _ask(path))
    assert (code, error["code"]) == (2, expected)


def test_an_unloadable_profile_fails_before_any_snapshot_is_read(tmp_path, capsys) -> None:
    code, error = _failure(
        capsys,
        lambda: _ask(missing_profile_config(tmp_path), snapshot_path=tmp_path / "missing.json"),
    )
    expected = (2, "provider_profile_unavailable", MISSING_PROFILE, "bedrock")
    assert (code, error["code"], error["profile"], error["kind"]) == expected


def test_an_unusable_transcript_is_reported(tmp_path: Path, capsys) -> None:
    config = fake_config(tmp_path, [text_turn()])
    (tmp_path / "run.json").write_text("{not json", encoding="utf-8")
    code, error = _failure(capsys, lambda: _ask(config))
    assert (code, error["code"]) == (2, "fake_transcript_invalid")


@pytest.mark.parametrize("side", ["snapshot_path", "comparison_path"])
def test_unknown_and_invalid_snapshots_are_refused(tmp_path: Path, capsys, side: str) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT)])
    missing = tmp_path / "missing.json"
    code, error = _failure(capsys, lambda: _ask(config, **{side: missing}))
    assert (code, error["code"], error["path"]) == (1, "snapshot_io_error", str(missing))
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version": "2"}', encoding="utf-8")
    code, error = _failure(capsys, lambda: _ask(config, **{side: invalid}))
    assert code == 2 and error["path"] == str(invalid)


# ---- transcripts -------------------------------------------------------------------


def _written(result: AskResult, path: Path) -> RunTranscript:
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text
    transcript = RunTranscript.model_validate_json(text)
    assert transcript == result.answer.transcript
    return transcript


def _no_run(monkeypatch) -> None:
    async def refused(*args, **kwargs):
        raise AssertionError("the run must not start")

    monkeypatch.setattr(ask_module, "answer_question_async", refused)


def test_a_transcript_is_written_on_request(tmp_path: Path) -> None:
    config = fake_config(
        tmp_path, [calls_turn(SEARCH), submit_turn("a", answer_proposal(network_session()))]
    )
    path = tmp_path / "transcript.json"
    result = _ask(config, transcript_path=path)
    assert _written(result, path).outcome.kind == "proposed"


@pytest.mark.parametrize(
    ("turns", "config"),
    [([fail_turn("provider_unavailable")], ""), ([calls_turn(SEARCH)], "max_requests = 1\n")],
    ids=["provider-error", "limit"],
)
def test_failed_runs_write_their_transcript(tmp_path: Path, turns, config: str) -> None:
    path = tmp_path / "transcript.json"
    result = _ask(fake_config(tmp_path, turns, config), transcript_path=path)
    assert result.answer.outcome.exit_code == 6
    assert _written(result, path).outcome == result.answer.transcript.outcome


def test_a_cancelled_run_writes_its_transcript(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "transcript.json"
    result = cancelled_ask(tmp_path, monkeypatch, transcript_path=path)
    assert _written(result, path).outcome.kind == "cancelled"


def test_a_failed_save_does_not_hide_the_cancellation(tmp_path: Path, monkeypatch) -> None:
    def unwritable(text: str, path: Path, *, subject: str) -> Path:
        raise SnapshotIOError("Unable to save transcript.", path=path)

    monkeypatch.setattr(ask_module, "publish_text", unwritable)
    result = cancelled_ask(tmp_path, monkeypatch, transcript_path=tmp_path / "transcript.json")
    assert result.answer.outcome.exit_code == 130


def test_an_existing_transcript_path_is_refused_before_the_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT)])
    path = tmp_path / "transcript.json"
    path.write_text("kept", encoding="utf-8")
    _no_run(monkeypatch)
    code, error = _failure(capsys, lambda: _ask(config, transcript_path=path))
    assert (code, error["code"], error["path"]) == (3, "snapshot_conflict", str(path))
    assert path.read_text(encoding="utf-8") == "kept"


def test_a_transcript_path_without_a_parent_is_refused_before_the_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT)])
    path = tmp_path / "missing" / "transcript.json"
    _no_run(monkeypatch)
    code, error = _failure(capsys, lambda: _ask(config, transcript_path=path))
    assert (code, error["code"], error["path"]) == (1, "snapshot_io_error", str(path))


def test_no_transcript_is_written_when_the_run_does_not_start(tmp_path: Path, capsys) -> None:
    config = fake_config(tmp_path, [text_turn(PLAIN_TEXT)])
    path = tmp_path / "transcript.json"
    code, _ = _failure(
        capsys,
        lambda: _ask(config, snapshot_path=tmp_path / "missing.json", transcript_path=path),
    )
    assert code == 1 and not path.exists()
