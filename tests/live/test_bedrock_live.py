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

import os
from pathlib import Path

import boto3
import pytest

from tests.support.live import ask_one_question, write_config

pytestmark = pytest.mark.live

PROFILE = "ARCHDISCOV_LIVE_BEDROCK_PROFILE"
REGION = "ARCHDISCOV_LIVE_BEDROCK_REGION"
MODEL = "ARCHDISCOV_LIVE_BEDROCK_MODEL"
ROLE_ARN = "ARCHDISCOV_LIVE_BEDROCK_ROLE_ARN"
REQUIRED = (PROFILE, REGION, MODEL)


def test_one_tool_using_question_is_answered_or_explained(tmp_path: Path) -> None:
    if not all(os.environ.get(name) for name in REQUIRED):
        pytest.skip(f"set {', '.join(REQUIRED)} to run the live Bedrock test")
    fields = {
        "kind": "bedrock",
        "model": os.environ[MODEL],
        "profile": os.environ[PROFILE],
        "region": os.environ[REGION],
    }
    if os.environ.get(ROLE_ARN):
        fields["role_arn"] = os.environ[ROLE_ARN]
    credentials = boto3.Session(profile_name=os.environ[PROFILE]).get_credentials()
    if credentials is None:
        pytest.skip(f"{PROFILE} names a profile without credentials")
    secrets = [credentials.secret_key, credentials.token]
    config = write_config(tmp_path / "provider.toml", fields)
    ask_one_question(tmp_path, config, "bedrock", *(secret for secret in secrets if secret))
