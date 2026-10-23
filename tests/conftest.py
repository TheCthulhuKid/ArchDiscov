"""Fixtures for every test."""

import os

import pytest

# Variables through which botocore would read the developer's AWS profiles or credentials.
_AWS_VARIABLES = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


@pytest.fixture(autouse=True)
def no_aws_configuration(request, monkeypatch) -> None:
    """No test reads the developer's AWS configuration; live tests opt out."""
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in _AWS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
