"""Opt-in smoke test of the Bedrock adapter against the real Converse API.

It is marked `live`, which the default pytest options deselect, so normal runs and CI never
collect it, and it skips unless `ARCHDISCOV_LIVE_BEDROCK_PROFILE`,
`ARCHDISCOV_LIVE_BEDROCK_REGION`, and `ARCHDISCOV_LIVE_BEDROCK_MODEL` name the AWS profile,
region, and model (a model ID, inference profile ID, or ARN that supports tool use).
`ARCHDISCOV_LIVE_BEDROCK_ROLE_ARN` optionally names a role to assume. The caps match the
other live tests, and the profile's secret key and session token must not reach any output.
Run it only on purpose:

    ARCHDISCOV_LIVE_BEDROCK_PROFILE=assistant \
    ARCHDISCOV_LIVE_BEDROCK_REGION=eu-west-1 \
    ARCHDISCOV_LIVE_BEDROCK_MODEL=eu.anthropic.claude-sonnet-5-5-v1:0 \
    .venv/bin/python -m pytest -m live tests/live/test_bedrock_live.py
"""

from pathlib import Path

import pytest

from tests.support.live import ask_one_question, bedrock_provider, write_config

pytestmark = pytest.mark.live


def test_one_tool_using_question_is_answered_or_explained(tmp_path: Path) -> None:
    provider = bedrock_provider()
    config = write_config(tmp_path / "provider.toml", provider.fields)
    ask_one_question(tmp_path, config, provider.kind, *provider.secrets)
