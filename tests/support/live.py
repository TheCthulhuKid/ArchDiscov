"""Shared shape of the opt-in live provider tests: capped configurations and one question.

Each live test writes a provider configuration capped at `MAX_REQUESTS` requests (plus the
one repair) of 4,096 output tokens and one retry, asks one README question over an audit
scenario, and accepts a validated answer or an explained failure without the key in any
output.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.runs import RunTranscript
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import ask_question
from archdiscov.services.snapshots import save_snapshot
from tests.support.ask_examples import OWNERSHIP
from tests.support.audit_scenarios import SCENARIOS

MAX_REQUESTS = 4
_CAPS = {
    "max_requests": MAX_REQUESTS,
    "max_tool_calls": 4,
    "max_output_tokens": 4096,
    "max_retries": 1,
    "request_timeout_seconds": 120,
    "max_latency_seconds": 300,
}


def key_variable(key_env: str) -> str:
    """The variable `key_env` names; skips when it holds no key."""
    variable = os.environ[key_env]
    if not os.environ.get(variable, "").strip():
        pytest.skip(f"{key_env} names {variable}, which holds no key")
    return variable


def write_config(path: Path, fields: dict[str, Any]) -> Path:
    """A capped configuration; JSON strings, numbers, and booleans are also TOML."""
    lines = (f"{key} = {json.dumps(value)}" for key, value in {**fields, **_CAPS}.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def ask_one_question(tmp_path: Path, config: Path, provider_kind: str, key: str) -> None:
    """Ask the ownership question and accept a validated answer or an explained failure."""
    snapshot = tmp_path / "snapshot.json"
    save_snapshot(SCENARIOS[AnalyzerKind.OWNERSHIP].build(), snapshot)
    transcript = tmp_path / "transcript.json"
    result = ask_question(
        OWNERSHIP, snapshot_path=snapshot, config_path=config, transcript_path=transcript
    )
    answer, summary = result.answer, result.summary
    assert summary.provider_kind == provider_kind
    assert 1 <= summary.usage.provider_requests <= MAX_REQUESTS + 1
    if answer.outcome.failure is None:
        # A validated answer cites only what the tools returned, so tools were used.
        assert answer.rendered.conclusions and summary.usage.tool_calls
    else:
        assert answer.rendered.failure is not None
    RunTranscript.model_validate_json(transcript.read_text(encoding="utf-8"))
    assert key not in result.model_dump_json() and key not in transcript.read_text("utf-8")
