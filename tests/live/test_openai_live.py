"""Opt-in smoke tests of the OpenAI-compatible adapter against a real API, per protocol.

They are marked `live`, which the default pytest options deselect, so normal runs and CI
never collect them. Each protocol's test skips unless `ARCHDISCOV_LIVE_OPENAI_KEY_ENV`
names the environment variable that holds a key, `ARCHDISCOV_LIVE_OPENAI_ENDPOINT` and
`ARCHDISCOV_LIVE_OPENAI_MODEL` name the API root and model, and
`ARCHDISCOV_LIVE_OPENAI_PROTOCOLS` lists that protocol (comma-separated). Setting
`ARCHDISCOV_LIVE_OPENAI_ENCRYPTED_REASONING=1` declares `encrypted_reasoning` for the
Responses run, for reasoning models. The caps match the Anthropic test. Run them only on
purpose:

    ARCHDISCOV_LIVE_OPENAI_KEY_ENV=OPENAI_API_KEY \
    ARCHDISCOV_LIVE_OPENAI_ENDPOINT=https://api.openai.com/v1 \
    ARCHDISCOV_LIVE_OPENAI_MODEL=gpt-5 \
    ARCHDISCOV_LIVE_OPENAI_PROTOCOLS=responses,chat_completions \
    .venv/bin/python -m pytest -m live tests/live/test_openai_live.py
"""

import os
from pathlib import Path

import pytest

from archdiscov.assistant.config import OpenAIProtocol
from tests.support.live import ask_one_question, key_variable, write_config

pytestmark = pytest.mark.live

KEY_ENV = "ARCHDISCOV_LIVE_OPENAI_KEY_ENV"
ENDPOINT = "ARCHDISCOV_LIVE_OPENAI_ENDPOINT"
MODEL = "ARCHDISCOV_LIVE_OPENAI_MODEL"
PROTOCOLS = "ARCHDISCOV_LIVE_OPENAI_PROTOCOLS"
ENCRYPTED_REASONING = "ARCHDISCOV_LIVE_OPENAI_ENCRYPTED_REASONING"
REQUIRED = (KEY_ENV, ENDPOINT, MODEL, PROTOCOLS)


@pytest.mark.parametrize("protocol", list(OpenAIProtocol), ids=str)
def test_one_tool_using_question_is_answered_or_explained(
    tmp_path: Path, protocol: OpenAIProtocol
) -> None:
    if not all(os.environ.get(name) for name in REQUIRED):
        pytest.skip(f"set {', '.join(REQUIRED)} to run the live OpenAI-compatible tests")
    if protocol.value not in {item.strip() for item in os.environ[PROTOCOLS].split(",")}:
        pytest.skip(f"{PROTOCOLS} does not list {protocol.value}")
    variable = key_variable(KEY_ENV)
    fields = {
        "kind": "openai_compatible",
        "model": os.environ[MODEL],
        "api_key_env": variable,
        "endpoint": os.environ[ENDPOINT],
        "protocol": protocol.value,
    }
    if protocol is OpenAIProtocol.RESPONSES and os.environ.get(ENCRYPTED_REASONING) == "1":
        fields["encrypted_reasoning"] = True
    config = write_config(tmp_path / "provider.toml", fields)
    ask_one_question(tmp_path, config, "openai_compatible", os.environ[variable])
