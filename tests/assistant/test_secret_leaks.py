"""A credential never appears in a repr, JSON dump, or exception from the assistant modules."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from archdiscov.assistant.config import (
    load_provider_config,
    parse_provider_config,
    resolve_api_key,
)
from archdiscov.assistant.conversation import parse_conversation
from archdiscov.assistant.fake import load_fake_transcript
from archdiscov.assistant.provider import ProviderAuthenticationError
from tests.support.secrets import SECRET, secret_renderings

ANTHROPIC = {"kind": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ARCHDISCOV_KEY"}


def _raised(action: Callable[[], object]) -> BaseException:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - the test inspects whatever is raised
        return exc
    raise AssertionError("expected an exception")


def _resolved_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    monkeypatch.setenv("ARCHDISCOV_KEY", SECRET)
    config = parse_provider_config(ANTHROPIC)
    return [config, resolve_api_key(config), config.request_limits()]


def _config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "anthropic"\nmodel = "m"\napi_key_env = "K"\napi_key = "{SECRET}"\n',
        encoding="utf-8",
    )
    return [_raised(lambda: load_provider_config(path))]


def _config_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    return [
        _raised(lambda: parse_provider_config({**ANTHROPIC, "model": SECRET})),
        _raised(lambda: parse_provider_config({"kind": SECRET})),
    ]


def _provider_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    error = ProviderAuthenticationError()
    error.__cause__ = RuntimeError(f"Authorization: Bearer {SECRET}")
    return [error]


def _conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    return [
        _raised(lambda: parse_conversation({"messages": [{"role": "user", SECRET: SECRET}]})),
        _raised(lambda: parse_conversation({"messages": [{"role": SECRET, "text": SECRET}]})),
    ]


def _transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"turns": [{"kind": "fail", "error": SECRET}]}), encoding="utf-8")
    return [_raised(lambda: load_fake_transcript(path))]


@pytest.mark.parametrize(
    "scenario",
    [
        _resolved_key,
        _config_file,
        _config_values,
        _provider_error,
        _conversation,
        _transcript,
    ],
    ids=lambda scenario: scenario.__name__.strip("_"),
)
def test_secret_never_rendered(scenario, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = scenario(monkeypatch, tmp_path)
    assert values
    for value in values:
        for rendered in secret_renderings(value):
            assert SECRET not in rendered, (scenario.__name__, type(value).__name__)
