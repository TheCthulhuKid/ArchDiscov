"""Shared shape of the opt-in live provider tests: capped configurations and one question.

Each live provider is configured from its `ARCHDISCOV_LIVE_*` variables by a builder that
skips with its reason when they are not set. Each live test writes a provider configuration
capped at `MAX_REQUESTS` requests (plus the one repair) of 4,096 output tokens and one
retry, asks one README question over an audit scenario, and accepts a validated answer or
an explained failure without the key (or, for Bedrock, the profile's secrets) in any
output. The live evaluation runs the README suite through every configured provider.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import boto3
import pytest

from archdiscov.assistant.config import OpenAIProtocol
from archdiscov.assistant.runs import RunTranscript
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import ask_question
from archdiscov.services.snapshots import save_snapshot
from tests.support.ask_examples import OWNERSHIP, write_provider_config
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


ANTHROPIC_KEY_ENV = "ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV"
ANTHROPIC_MODEL = "ARCHDISCOV_LIVE_ANTHROPIC_MODEL"
OPENAI_KEY_ENV = "ARCHDISCOV_LIVE_OPENAI_KEY_ENV"
OPENAI_ENDPOINT = "ARCHDISCOV_LIVE_OPENAI_ENDPOINT"
OPENAI_MODEL = "ARCHDISCOV_LIVE_OPENAI_MODEL"
OPENAI_PROTOCOLS = "ARCHDISCOV_LIVE_OPENAI_PROTOCOLS"
OPENAI_ENCRYPTED_REASONING = "ARCHDISCOV_LIVE_OPENAI_ENCRYPTED_REASONING"
BEDROCK_PROFILE = "ARCHDISCOV_LIVE_BEDROCK_PROFILE"
BEDROCK_REGION = "ARCHDISCOV_LIVE_BEDROCK_REGION"
BEDROCK_MODEL = "ARCHDISCOV_LIVE_BEDROCK_MODEL"
BEDROCK_ROLE_ARN = "ARCHDISCOV_LIVE_BEDROCK_ROLE_ARN"


@dataclass(frozen=True)
class LiveProvider:
    """A configured live provider: its configuration fields and the secrets no output may
    hold."""

    kind: str
    fields: dict[str, Any]
    secrets: tuple[str, ...]


def _require(*names: str, provider: str) -> None:
    if not all(os.environ.get(name) for name in names):
        pytest.skip(f"set {', '.join(names)} to run the live {provider} tests")


def anthropic_provider() -> LiveProvider:
    _require(ANTHROPIC_KEY_ENV, ANTHROPIC_MODEL, provider="Anthropic")
    variable = key_variable(ANTHROPIC_KEY_ENV)
    fields = {"kind": "anthropic", "model": os.environ[ANTHROPIC_MODEL], "api_key_env": variable}
    return LiveProvider("anthropic", fields, (os.environ[variable],))


def openai_provider(protocol: OpenAIProtocol) -> LiveProvider:
    _require(
        OPENAI_KEY_ENV,
        OPENAI_ENDPOINT,
        OPENAI_MODEL,
        OPENAI_PROTOCOLS,
        provider="OpenAI-compatible",
    )
    if protocol.value not in {item.strip() for item in os.environ[OPENAI_PROTOCOLS].split(",")}:
        pytest.skip(f"{OPENAI_PROTOCOLS} does not list {protocol.value}")
    variable = key_variable(OPENAI_KEY_ENV)
    fields = {
        "kind": "openai_compatible",
        "model": os.environ[OPENAI_MODEL],
        "api_key_env": variable,
        "endpoint": os.environ[OPENAI_ENDPOINT],
        "protocol": protocol.value,
    }
    if protocol is OpenAIProtocol.RESPONSES and os.environ.get(OPENAI_ENCRYPTED_REASONING) == "1":
        fields["encrypted_reasoning"] = True
    return LiveProvider("openai_compatible", fields, (os.environ[variable],))


def bedrock_provider() -> LiveProvider:
    _require(BEDROCK_PROFILE, BEDROCK_REGION, BEDROCK_MODEL, provider="Bedrock")
    fields = {
        "kind": "bedrock",
        "model": os.environ[BEDROCK_MODEL],
        "profile": os.environ[BEDROCK_PROFILE],
        "region": os.environ[BEDROCK_REGION],
    }
    if os.environ.get(BEDROCK_ROLE_ARN):
        fields["role_arn"] = os.environ[BEDROCK_ROLE_ARN]
    credentials = boto3.Session(profile_name=os.environ[BEDROCK_PROFILE]).get_credentials()
    if credentials is None:
        pytest.skip(f"{BEDROCK_PROFILE} names a profile without credentials")
    secrets = (credentials.secret_key, credentials.token)
    return LiveProvider("bedrock", fields, tuple(secret for secret in secrets if secret))


LIVE_PROVIDERS: tuple[Callable[[], LiveProvider], ...] = (
    anthropic_provider,
    *(partial(openai_provider, protocol) for protocol in OpenAIProtocol),
    bedrock_provider,
)


def configured_providers() -> list[LiveProvider]:
    """Every live provider whose variables are set; the others are left out."""
    providers = []
    for build in LIVE_PROVIDERS:
        try:
            providers.append(build())
        except pytest.skip.Exception:
            continue
    return providers


def key_variable(key_env: str) -> str:
    """The variable `key_env` names; skips when it holds no key."""
    variable = os.environ[key_env]
    if not os.environ.get(variable, "").strip():
        pytest.skip(f"{key_env} names {variable}, which holds no key")
    return variable


def write_config(path: Path, fields: dict[str, Any]) -> Path:
    """A configuration of `fields` with the live caps."""
    return write_provider_config(path, {**fields, **_CAPS})


def ask_one_question(tmp_path: Path, config: Path, provider_kind: str, *secrets: str) -> None:
    """Ask the ownership question and accept a validated answer or an explained failure;
    none of `secrets` reaches the result or the transcript."""
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
    recorded = transcript.read_text(encoding="utf-8")
    RunTranscript.model_validate_json(recorded)
    for secret in secrets:
        assert secret not in result.model_dump_json() and secret not in recorded
