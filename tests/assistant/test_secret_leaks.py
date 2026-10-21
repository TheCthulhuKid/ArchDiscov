"""A credential never appears in a repr, JSON dump, or exception from the assistant modules."""

import json
import traceback
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
from archdiscov.assistant.provider import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderTransportError,
)

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
ANTHROPIC = {"kind": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ARCHDISCOV_KEY"}


def _renderings(value: object) -> list[str]:
    rendered = [repr(value), str(value)]
    if hasattr(value, "model_dump_json"):
        rendered += [value.model_dump_json(), json.dumps(value.model_dump(mode="json"))]
    if isinstance(value, BaseException):
        rendered.append("".join(traceback.format_exception_only(value)))
        rendered += [repr(getattr(value, name, None)) for name in ("details", "args")]
        if isinstance(value, ProviderError):
            rendered.append(json.dumps(value.diagnostic()))
    return rendered


def _raised(action: Callable[[], object]) -> BaseException:
    try:
        action()
    except Exception as exc:
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
        _raised(lambda: parse_provider_config({**ANTHROPIC, "unknown": SECRET})),
        _raised(lambda: parse_provider_config({"kind": SECRET})),
        _raised(lambda: parse_provider_config({**ANTHROPIC, "max_retries": SECRET})),
    ]


def _missing_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    config = parse_provider_config(ANTHROPIC)
    return [
        _raised(lambda: resolve_api_key(config, {})),
        _raised(lambda: resolve_api_key(config, {"ARCHDISCOV_KEY": " "})),
    ]


def _provider_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    def mapped(error_type: type[ProviderError]) -> ProviderError:
        try:
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        except RuntimeError as cause:
            error = error_type()
            error.__cause__ = cause
            return error

    return [mapped(ProviderAuthenticationError), mapped(ProviderTransportError)]


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
        _missing_variable,
        _provider_errors,
        _conversation,
        _transcript,
    ],
    ids=lambda scenario: scenario.__name__.strip("_"),
)
def test_secret_never_rendered(scenario, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = scenario(monkeypatch, tmp_path)
    assert values
    for value in values:
        for rendered in _renderings(value):
            assert SECRET not in rendered, (scenario.__name__, type(value).__name__)
