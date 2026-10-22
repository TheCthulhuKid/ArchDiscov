"""The ask service: configuration to provider, snapshots to session, and a summarised answer.

Runs use fake configurations written next to their transcripts; errors before the run go
through the CLI diagnostics so their safe output and exit codes are checked as users see them.
"""

import asyncio
import json
from pathlib import Path

import pytest
import typer

from archdiscov.assistant.answer_results import FAILURE_EXIT_CODES, FailureReason
from archdiscov.assistant.runs import RunLimits, RunTranscript
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.cli_diagnostics import safe_assistant_failures
from archdiscov.services import ask as ask_module
from archdiscov.services.ask import AskCancelled, AskResult, ToolUsage, ask_question
from archdiscov.services.snapshot_errors import SnapshotIOError
from tests.assistant.test_answer_service import PLAIN_TEXT, SEARCH, _proposal, _rejected, _submit
from tests.assistant.test_orchestrator import _calls, _fail, _text
from tests.assistant.test_orchestrator_async import _HangingProvider
from tests.assistant.test_secret_leaks import SECRET
from tests.paths import FIXTURES_ROOT

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "network.json"
COMPARISON = FIXTURES_ROOT / "snapshots" / "aws.json"
QUESTION = f"Which resources are in the network? ({SECRET})"


def _fake(tmp_path: Path, turns: list[dict], config: str = "") -> Path:
    (tmp_path / "run.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")
    path = tmp_path / "provider.toml"
    path.write_text(f'kind = "fake"\ntranscript = "run.json"\n{config}', encoding="utf-8")
    return path


def _ask(config_path: Path, **paths: Path) -> AskResult:
    return ask_question(
        QUESTION,
        snapshot_path=paths.get("snapshot_path", SNAPSHOT),
        config_path=config_path,
        comparison_path=paths.get("comparison_path"),
        transcript_path=paths.get("transcript_path"),
    )


def _session() -> ToolSession:
    return ToolSession.from_paths(SNAPSHOT)


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
    config = _fake(tmp_path, [_calls(SEARCH), _submit("a", _proposal(_session()))])
    result = _ask(config)
    assert result.answer.outcome.status == "answered" and result.answer.outcome.exit_code == 0
    usage = result.summary.usage
    assert usage.provider_requests == 2 and usage.tool_calls == {"search_resources": 1}
    assert not usage.repair_requested
    assert result.summary.limits == RunLimits()
    _check_summary(result)


def test_a_repaired_answer_records_the_repair(tmp_path: Path) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT), _submit("a", _proposal(_session()))])
    result = _ask(config)
    assert result.answer.outcome.status == "answered_after_repair"
    assert result.summary.usage.repair_requested
    _check_summary(result)


@pytest.mark.parametrize(
    ("turns", "config", "reason"),
    [
        (
            lambda: [_submit("a", _rejected(_session())), _submit("b", _rejected(_session()))],
            "",
            FailureReason.REJECTED_AFTER_REPAIR,
        ),
        (lambda: [_fail("provider_unavailable")], "", FailureReason.PROVIDER_ERROR),
        (lambda: [_calls(SEARCH)], "max_requests = 1\n", FailureReason.LIMIT_REACHED),
    ],
    ids=["exit-5", "exit-6-provider", "exit-6-limit"],
)
def test_failures_are_outcomes(tmp_path: Path, turns, config: str, reason: FailureReason) -> None:
    config_path = _fake(tmp_path, turns(), config)
    result = _ask(config_path)
    outcome = result.answer.outcome
    assert outcome.failure == reason and outcome.exit_code == FAILURE_EXIT_CODES[reason]
    assert result.answer.rendered.failure.reason == reason
    _check_summary(result)


def test_configured_limits_bound_the_run(tmp_path: Path) -> None:
    config = _fake(tmp_path, [_calls(SEARCH)], "max_requests = 1\nmax_tool_calls = 4\n")
    limits = _ask(config).summary.limits
    assert (limits.max_provider_requests, limits.max_tool_calls) == (1, 4)


def test_a_comparison_snapshot_is_summarised_as_the_second_side(tmp_path: Path) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT), _text(PLAIN_TEXT)])
    result = _ask(config, comparison_path=COMPARISON)
    assert [item.side for item in result.summary.snapshots] == ["primary", "comparison"]
    _check_summary(result)


def _cancelled(tmp_path: Path, monkeypatch, transcript_path: Path | None = None) -> AskResult:
    config_path = _fake(tmp_path, [_text()])
    provider = _HangingProvider(answer_first=True)
    built = ask_module.load_provider

    def hanging(path: Path, *, environ=None):
        config, _ = built(path, environ=environ)
        return config, provider

    monkeypatch.setattr(ask_module, "load_provider", hanging)

    async def scenario() -> AskResult:
        task = asyncio.create_task(
            ask_module.ask_question_async(
                QUESTION,
                snapshot_path=SNAPSHOT,
                config_path=config_path,
                transcript_path=transcript_path,
            )
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(AskCancelled) as raised:
            await task
        return raised.value.result

    return asyncio.run(scenario())


def test_cancelling_explains_the_cancelled_question(tmp_path: Path, monkeypatch) -> None:
    result = _cancelled(tmp_path, monkeypatch)
    assert result.answer.outcome.failure == "cancelled"
    assert result.answer.outcome.exit_code == 130
    _check_summary(result)


# ---- errors before the run ---------------------------------------------------------


def _failure(capsys, action) -> tuple[int, dict]:
    with pytest.raises(typer.Exit) as raised:
        with safe_assistant_failures(SNAPSHOT, as_json=True):
            action()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    return raised.value.exit_code, json.loads(output.out)["error"]


def _unadapted(tmp_path: Path) -> Path:
    path = tmp_path / "provider.toml"
    path.write_text('kind = "anthropic"\nmodel = "m"\napi_key_env = "ASK_KEY"\n', encoding="utf-8")
    return path


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


def test_a_kind_without_an_adapter_fails_before_any_snapshot_is_read(tmp_path, capsys) -> None:
    code, error = _failure(
        capsys, lambda: _ask(_unadapted(tmp_path), snapshot_path=tmp_path / "missing.json")
    )
    assert (code, error["code"], error["kind"]) == (2, "provider_adapter_unavailable", "anthropic")


def test_an_unusable_transcript_is_reported(tmp_path: Path, capsys) -> None:
    config = _fake(tmp_path, [_text()])
    (tmp_path / "run.json").write_text("{not json", encoding="utf-8")
    code, error = _failure(capsys, lambda: _ask(config))
    assert (code, error["code"]) == (2, "fake_transcript_invalid")


@pytest.mark.parametrize("side", ["snapshot_path", "comparison_path"])
def test_unknown_and_invalid_snapshots_are_refused(tmp_path: Path, capsys, side: str) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT)])
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
    config = _fake(tmp_path, [_calls(SEARCH), _submit("a", _proposal(_session()))])
    path = tmp_path / "transcript.json"
    result = _ask(config, transcript_path=path)
    assert _written(result, path).outcome.kind == "proposed"


@pytest.mark.parametrize(
    ("turns", "config"),
    [([_fail("provider_unavailable")], ""), ([_calls(SEARCH)], "max_requests = 1\n")],
    ids=["provider-error", "limit"],
)
def test_failed_runs_write_their_transcript(tmp_path: Path, turns, config: str) -> None:
    path = tmp_path / "transcript.json"
    result = _ask(_fake(tmp_path, turns, config), transcript_path=path)
    assert result.answer.outcome.exit_code == 6
    assert _written(result, path).outcome == result.answer.transcript.outcome


def test_a_cancelled_run_writes_its_transcript(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "transcript.json"
    result = _cancelled(tmp_path, monkeypatch, transcript_path=path)
    assert _written(result, path).outcome.kind == "cancelled"


def test_a_failed_save_does_not_hide_the_cancellation(tmp_path: Path, monkeypatch) -> None:
    def unwritable(text: str, path: Path, *, subject: str) -> Path:
        raise SnapshotIOError("Unable to save transcript.", path=path)

    monkeypatch.setattr(ask_module, "publish_text", unwritable)
    result = _cancelled(tmp_path, monkeypatch, transcript_path=tmp_path / "transcript.json")
    assert result.answer.outcome.exit_code == 130


@pytest.mark.parametrize("kind", ["file", "dangling-symlink"])
def test_an_existing_transcript_path_is_refused_before_the_run(
    tmp_path: Path, capsys, monkeypatch, kind: str
) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT)])
    path = tmp_path / "transcript.json"
    if kind == "file":
        path.write_text("kept", encoding="utf-8")
    else:
        path.symlink_to(tmp_path / "missing-target.json")
    _no_run(monkeypatch)
    code, error = _failure(capsys, lambda: _ask(config, transcript_path=path))
    assert (code, error["code"], error["path"]) == (3, "snapshot_conflict", str(path))
    assert path.is_symlink() or path.read_text(encoding="utf-8") == "kept"


def test_a_transcript_path_without_a_parent_is_refused_before_the_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT)])
    path = tmp_path / "missing" / "transcript.json"
    _no_run(monkeypatch)
    code, error = _failure(capsys, lambda: _ask(config, transcript_path=path))
    assert (code, error["code"], error["path"]) == (1, "snapshot_io_error", str(path))


def test_no_transcript_is_written_when_the_run_does_not_start(tmp_path: Path, capsys) -> None:
    config = _fake(tmp_path, [_text(PLAIN_TEXT)])
    path = tmp_path / "transcript.json"
    code, _ = _failure(
        capsys,
        lambda: _ask(config, snapshot_path=tmp_path / "missing.json", transcript_path=path),
    )
    assert code == 1 and not path.exists()
