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

from pathlib import Path

import pytest

from archdiscov.assistant.config import OpenAIProtocol
from tests.support.live import ask_one_question, openai_provider, write_config

pytestmark = pytest.mark.live


@pytest.mark.parametrize("protocol", list(OpenAIProtocol), ids=str)
def test_one_tool_using_question_is_answered_or_explained(
    tmp_path: Path, protocol: OpenAIProtocol
) -> None:
    provider = openai_provider(protocol)
    config = write_config(tmp_path / "provider.toml", provider.fields)
    ask_one_question(tmp_path, config, provider.kind, *provider.secrets)
