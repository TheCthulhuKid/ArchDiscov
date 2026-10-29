"""Running an evaluation suite: everything checked before a request, every case recorded.

Suites are the README examples, written with their files into a temporary directory, run
with the fake provider or an adapter over a mocked API; nothing reaches the network.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.config import ProviderConfigError, ProviderConfigErrorCode
from archdiscov.assistant.evaluation import (
    MAX_PROVIDERS,
    MAX_SUITE_BYTES,
    Budget,
    EvaluationErrorCode,
    EvaluationReport,
    EvaluationValidationError,
    parse_report,
)
from archdiscov.assistant.fake import FakeScriptError, FakeScriptErrorCode
from archdiscov.assistant.provider import ProviderCapabilities, ProviderCapability
from archdiscov.assistant.runs import RunLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.services.evaluation import (
    EvaluationCancelled,
    EvaluationSetupCode,
    EvaluationSetupError,
    _Provider,
    evaluate_suite,
    prepare_evaluation,
    run_evaluation,
    run_evaluation_async,
)
from archdiscov.services.snapshot_errors import SnapshotIOError
from tests.support.evaluation_suites import (
    EVALUATION_KEY_VARIABLE,
    SUITE,
    anthropic_config,
    serve_suite,
    write_fake_config,
    write_readme_suite,
)
from tests.support.provider_harness import AnthropicHarness
from tests.support.scripted_turns import SNAPSHOT

NAMES = ["internet_exposure", "ownership", "ownership_compared"]
NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.fixture(scope="module")
def suite(tmp_path_factory) -> Path:
    return write_readme_suite(tmp_path_factory.mktemp("suite"), NAMES)


def _fake(directory: Path, name: str = "fake.toml", **fields: Any) -> Path:
    return write_fake_config(directory / name, **fields)


def _edited_suite(suite: Path, name: str, edit) -> Path:
    """A copy of `suite`, beside it and its case files, changed by `edit`."""
    data = json.loads(suite.read_text(encoding="utf-8"))
    edit(data)
    path = suite.parent / f"{name}-{SUITE}"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _beside(suite: Path, name: str, content: str) -> str:
    """A file written beside the suite, as a suite path."""
    (suite.parent / name).write_text(content, encoding="utf-8")
    return name


def _without_timing(report: EvaluationReport) -> dict:
    data = report.model_dump(mode="json", exclude={"created_at"})
    for run in data["providers"]:
        run["totals"].pop("latency_seconds")
        for record in run["records"]:
            record.pop("latency_seconds")
    return data


# ---- running -----------------------------------------------------------------------


def test_a_fake_evaluation_is_deterministic_and_passes(suite: Path, tmp_path: Path) -> None:
    config = _fake(tmp_path)
    first = evaluate_suite(suite, [config], now=lambda: NOW)
    second = evaluate_suite(suite, [config])
    assert _without_timing(first) == _without_timing(second)
    assert parse_report(first.model_dump(mode="json")) == first
    assert first.created_at == NOW
    [run] = first.providers
    assert [record.case_id for record in run.records] == NAMES
    assert all(record.passed for record in run.records), [r.checks for r in run.records]
    assert (run.totals.cases, run.totals.passed, run.provider_kind) == (3, 3, "fake")
    assert not run.cancelled


def test_every_run_has_a_fresh_record(suite: Path, tmp_path: Path) -> None:
    """Two fake configurations score the same: no run sees another run's returned IDs."""
    configs = [_fake(tmp_path, "a.toml"), _fake(tmp_path, "b.toml")]
    prepared = prepare_evaluation(suite, configs)
    first, second = run_evaluation(prepared).providers
    assert [r.id for r in first.records] == [r.id for r in second.records]
    # Runs record into copies; the prepared sessions keep empty records.
    empty = ToolSession.from_paths(SNAPSHOT).record.returned()
    assert all(session.record.returned() == empty for session in prepared.sessions)
    session = ToolSession.from_paths(SNAPSHOT)
    fresh = session.fresh()
    assert fresh.index is session.index and fresh.record is not session.record


def test_a_failing_case_does_not_stop_later_cases(suite: Path, tmp_path: Path) -> None:
    failing = _beside(
        suite,
        "failing.json",
        json.dumps({"turns": [{"kind": "fail", "error": "provider_unavailable"}]}),
    )

    def fail_first(data: dict) -> None:
        data["cases"][0]["fake_transcript"] = failing

    edited = _edited_suite(suite, "failing", fail_first)
    [run] = evaluate_suite(edited, [_fake(tmp_path)]).providers
    first, *rest = run.records
    assert first.outcome.failure == "provider_error" and not first.passed
    assert len(rest) == 2 and all(record.passed for record in rest)
    assert (run.totals.passed, run.totals.failed) == (2, 1)


def test_the_budget_matches_each_configuration(suite: Path, tmp_path: Path) -> None:
    small = _fake(tmp_path, "small.toml", max_requests=4, max_output_tokens=1_000)
    prepared = prepare_evaluation(suite, [small, _fake(tmp_path)])
    assert prepared.budgets == (
        Budget(runs=3, max_provider_requests=15, max_output_tokens=15_000),
        Budget.for_runs(RunLimits(), 3),
    )
    report = evaluate_suite(suite, [small, _fake(tmp_path)])
    assert tuple(run.budget for run in report.providers) == prepared.budgets


def test_an_adapter_reaches_the_same_records_as_the_fake(
    suite: Path, tmp_path: Path, monkeypatch
) -> None:
    harness = AnthropicHarness()
    api = serve_suite(harness, suite, monkeypatch)
    report = evaluate_suite(
        suite,
        [_fake(tmp_path), anthropic_config(tmp_path, harness)],
        allow_external=True,
        environ={EVALUATION_KEY_VARIABLE: "sk-ant-api03-EVALUATIONkey0123456789abcdef"},
    )
    fake, adapter = report.providers
    assert adapter.provider_kind == "anthropic"
    assert [r.id for r in adapter.records] == [r.id for r in fake.records]
    assert len(api.requests) == adapter.totals.provider_requests


# ---- before the run ----------------------------------------------------------------


def test_an_external_provider_needs_the_opt_in_before_anything_is_read(
    suite: Path, tmp_path: Path, monkeypatch
) -> None:
    harness = AnthropicHarness()
    api = serve_suite(harness, suite, monkeypatch)

    def unreadable(data: dict) -> None:
        for case in data["cases"]:
            case["snapshot"] = "missing/snapshot.json"

    edited = _edited_suite(suite, "unreadable", unreadable)
    config = anthropic_config(tmp_path, harness)
    with pytest.raises(EvaluationSetupError) as raised:
        prepare_evaluation(edited, [_fake(tmp_path), config])
    assert raised.value.code == EvaluationSetupCode.EXTERNAL_NOT_ALLOWED
    assert raised.value.path == config
    # Allowed, the provider is built without a request, and the snapshot fails next.
    with pytest.raises(SnapshotIOError):
        prepare_evaluation(
            edited,
            [config],
            allow_external=True,
            environ={EVALUATION_KEY_VARIABLE: "sk-ant-api03-EVALUATIONkey0123456789abcdef"},
        )
    assert api.requests == []


def test_an_external_provider_without_its_key_names_its_file(suite: Path, tmp_path: Path) -> None:
    config = anthropic_config(tmp_path, AnthropicHarness())
    with pytest.raises(ProviderConfigError) as raised:
        prepare_evaluation(suite, [config], allow_external=True, environ={})
    assert raised.value.code == ProviderConfigErrorCode.CREDENTIAL_MISSING
    assert raised.value.path == config


def test_a_missing_snapshot_of_a_later_case_fails_before_any_run(
    suite: Path, tmp_path: Path
) -> None:
    def last_missing(data: dict) -> None:
        data["cases"][-1]["snapshot"] = "missing/snapshot.json"

    with pytest.raises(SnapshotIOError):
        prepare_evaluation(_edited_suite(suite, "last-missing", last_missing), [_fake(tmp_path)])


def test_a_fake_configuration_needs_every_cases_transcript(suite: Path, tmp_path: Path) -> None:
    def no_transcript(data: dict) -> None:
        del data["cases"][1]["fake_transcript"]

    edited = _edited_suite(suite, "no-transcript", no_transcript)
    with pytest.raises(EvaluationSetupError) as raised:
        prepare_evaluation(edited, [_fake(tmp_path)])
    assert raised.value.code == EvaluationSetupCode.TRANSCRIPT_MISSING
    assert raised.value.case_id == NAMES[1]


def test_an_invalid_fake_transcript_fails_before_any_run(suite: Path, tmp_path: Path) -> None:
    broken = _beside(suite, "broken.json", '{"turns": "none"}')

    def broken_first(data: dict) -> None:
        data["cases"][0]["fake_transcript"] = broken

    edited = _edited_suite(suite, "broken", broken_first)
    with pytest.raises(FakeScriptError) as raised:
        prepare_evaluation(edited, [_fake(tmp_path)])
    assert raised.value.code == FakeScriptErrorCode.INVALID


@pytest.mark.parametrize("count", [0, MAX_PROVIDERS + 1])
def test_an_evaluation_needs_one_to_ten_configurations(
    suite: Path, tmp_path: Path, count: int
) -> None:
    with pytest.raises(EvaluationSetupError) as raised:
        prepare_evaluation(suite, [_fake(tmp_path)] * count)
    assert raised.value.code == EvaluationSetupCode.PROVIDERS_INVALID


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (None, EvaluationErrorCode.SUITE_UNREADABLE),
        (b" " * (MAX_SUITE_BYTES + 1), EvaluationErrorCode.SUITE_TOO_LARGE),
        (b'{"schema_version": "1", "schema_version": "1"}', EvaluationErrorCode.INVALID_SUITE),
        (b'{"schema_version": "1"}', EvaluationErrorCode.INVALID_SUITE),
    ],
    ids=["missing", "too-large", "not-json", "not-a-suite"],
)
def test_an_unusable_suite_file_fails_with_its_path(tmp_path: Path, content, code) -> None:
    path = tmp_path / SUITE
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(EvaluationValidationError) as raised:
        prepare_evaluation(path, [_fake(tmp_path)])
    assert raised.value.code == code and raised.value.path == path


# ---- cancellation ------------------------------------------------------------------


class _Hanging:
    """A provider whose request never finishes, until the evaluation is cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(capabilities={ProviderCapability.TOOL_CALLING})

    def complete(self, request):
        raise AssertionError("the evaluation runs asynchronously")

    async def complete_async(self, request):
        self.started.set()
        await asyncio.Event().wait()


def test_a_cancellation_ends_the_evaluation_with_the_records_so_far(
    suite: Path, tmp_path: Path
) -> None:
    prepared = prepare_evaluation(suite, [_fake(tmp_path, "a.toml"), _fake(tmp_path, "b.toml")])
    finished, second = prepared.providers
    hanging = _Hanging()
    prepared = replace(prepared, providers=(finished, _Provider(second.config, hanging)))

    async def cancel() -> EvaluationReport:
        task = asyncio.create_task(run_evaluation_async(prepared, now=lambda: NOW))
        await hanging.started.wait()
        task.cancel()
        with pytest.raises(EvaluationCancelled) as raised:
            await task
        return raised.value.report

    report = asyncio.run(cancel())
    done, interrupted = report.providers
    assert not done.cancelled and len(done.records) == len(NAMES)
    assert interrupted.cancelled
    [record] = interrupted.records
    assert record.case_id == NAMES[0] and record.outcome.failure == "cancelled"
    assert parse_report(report.model_dump(mode="json")) == report
