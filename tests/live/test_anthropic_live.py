"""Opt-in smoke test of the Anthropic adapter against the real Messages API.

It is marked `live`, which the default pytest options deselect, so normal runs and CI never
collect it, and it skips unless `ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV` names the environment
variable that holds a key and `ARCHDISCOV_LIVE_ANTHROPIC_MODEL` names the model. It asks one
README question over an audit scenario, capped at four requests (plus the one repair) of
4,096 output tokens each and one retry, and accepts a validated answer or an explained
failure. Run it only on purpose:

    ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV=ANTHROPIC_API_KEY \
    ARCHDISCOV_LIVE_ANTHROPIC_MODEL=claude-sonnet-5-5 \
    .venv/bin/python -m pytest -m live tests/live
"""

import json
import os
from pathlib import Path

import pytest

from archdiscov.assistant.runs import RunTranscript
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import ask_question
from archdiscov.services.snapshots import save_snapshot
from tests.support.ask_examples import OWNERSHIP
from tests.support.audit_scenarios import SCENARIOS

pytestmark = pytest.mark.live

KEY_ENV = "ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV"
MODEL = "ARCHDISCOV_LIVE_ANTHROPIC_MODEL"
MAX_REQUESTS = 4


@pytest.fixture
def live_config(tmp_path: Path) -> Path:
    variable, model = os.environ.get(KEY_ENV), os.environ.get(MODEL)
    if not variable or not model:
        pytest.skip(f"set {KEY_ENV} and {MODEL} to run the live Anthropic test")
    if not os.environ.get(variable, "").strip():
        pytest.skip(f"{KEY_ENV} names {variable}, which holds no key")
    path = tmp_path / "provider.toml"
    path.write_text(
        "\n".join(
            [
                'kind = "anthropic"',
                f"model = {json.dumps(model)}",
                f"api_key_env = {json.dumps(variable)}",
                f"max_requests = {MAX_REQUESTS}",
                "max_tool_calls = 4",
                "max_output_tokens = 4096",
                "max_retries = 1",
                "request_timeout_seconds = 120",
                "max_latency_seconds = 300",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_one_tool_using_question_is_answered_or_explained(tmp_path: Path, live_config) -> None:
    snapshot = tmp_path / "snapshot.json"
    save_snapshot(SCENARIOS[AnalyzerKind.OWNERSHIP].build(), snapshot)
    transcript = tmp_path / "transcript.json"
    result = ask_question(
        OWNERSHIP, snapshot_path=snapshot, config_path=live_config, transcript_path=transcript
    )
    answer, summary = result.answer, result.summary
    assert summary.provider_kind == "anthropic"
    assert 1 <= summary.usage.provider_requests <= MAX_REQUESTS + 1
    if answer.outcome.failure is None:
        # A validated answer cites only what the tools returned, so tools were used.
        assert answer.rendered.conclusions and summary.usage.tool_calls
    else:
        assert answer.rendered.failure is not None
    RunTranscript.model_validate_json(transcript.read_text(encoding="utf-8"))
    key = os.environ[os.environ[KEY_ENV]]
    assert key not in result.model_dump_json() and key not in transcript.read_text("utf-8")
