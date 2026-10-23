"""Opt-in smoke test of the Anthropic adapter against the real Messages API.

It is marked `live`, which the default pytest options deselect, so normal runs and CI never
collect it, and it skips unless `ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV` names the environment
variable that holds a key and `ARCHDISCOV_LIVE_ANTHROPIC_MODEL` names the model. It asks one
README question over an audit scenario, capped at four requests (plus the one repair) of
4,096 output tokens each and one retry, and accepts a validated answer or an explained
failure. Run it only on purpose:

    ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV=ANTHROPIC_API_KEY \
    ARCHDISCOV_LIVE_ANTHROPIC_MODEL=claude-sonnet-5-5 \
    .venv/bin/python -m pytest -m live tests/live/test_anthropic_live.py
"""

import os
from pathlib import Path

import pytest

from tests.support.live import ask_one_question, key_variable, write_config

pytestmark = pytest.mark.live

KEY_ENV = "ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV"
MODEL = "ARCHDISCOV_LIVE_ANTHROPIC_MODEL"


def test_one_tool_using_question_is_answered_or_explained(tmp_path: Path) -> None:
    if not os.environ.get(KEY_ENV) or not os.environ.get(MODEL):
        pytest.skip(f"set {KEY_ENV} and {MODEL} to run the live Anthropic test")
    variable = key_variable(KEY_ENV)
    config = write_config(
        tmp_path / "provider.toml",
        {"kind": "anthropic", "model": os.environ[MODEL], "api_key_env": variable},
    )
    ask_one_question(tmp_path, config, "anthropic", os.environ[variable])
