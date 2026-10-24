"""Building providers from configuration: every kind has an adapter."""

import json
import shutil
from pathlib import Path

import pytest

from archdiscov.assistant.config import (
    ProviderConfigError,
    ProviderConfigErrorCode,
    ProviderKind,
    parse_provider_config,
)
from archdiscov.assistant.fake import FakeProvider, FakeScriptError, FakeScriptErrorCode
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.providers import ADAPTERS, build_provider, load_provider
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import TESTS_ROOT
from tests.support.provider_data import (
    ANTHROPIC,
    BEDROCK,
    MISSING_PROFILE,
    OPENAI,
    missing_profile_config,
)
from tests.support.secrets import SECRET, secret_renderings

TRANSCRIPT = TESTS_ROOT / "fixtures" / "assistant" / "multi_step.json"
SNAPSHOT = TESTS_ROOT / "fixtures" / "snapshots" / "network.json"
EXAMPLES = {
    ProviderKind.ANTHROPIC: ANTHROPIC,
    ProviderKind.OPENAI_COMPATIBLE: OPENAI,
    ProviderKind.BEDROCK: BEDROCK,
}


def _config(directory: Path, body: str) -> Path:
    path = directory / "provider.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_every_kind_has_an_adapter_and_every_real_kind_an_example() -> None:
    assert set(ADAPTERS) == set(ProviderKind)
    assert set(EXAMPLES) == set(ProviderKind) - {ProviderKind.FAKE}


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_the_fake_is_built_from_its_transcript(tmp_path: Path, absolute: bool) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(TRANSCRIPT, scripts / "run.json")
    transcript = (scripts / "run.json") if absolute else Path("scripts/run.json")
    config_dir = tmp_path / "elsewhere" if absolute else tmp_path
    config_dir.mkdir(exist_ok=True)
    path = _config(config_dir, f'kind = "fake"\ntranscript = "{transcript.as_posix()}"\n')
    config, provider = load_provider(path)
    assert config.kind == ProviderKind.FAKE
    assert isinstance(provider, FakeProvider)
    turns = len(json.loads(TRANSCRIPT.read_text(encoding="utf-8"))["turns"])
    assert provider.remaining_turns == turns


def test_a_built_fake_answers_a_run(tmp_path: Path) -> None:
    turn = {
        "kind": "respond",
        "response": {"message": {"role": "assistant", "text": "Done."}, "stop_reason": "end"},
    }
    (tmp_path / "run.json").write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    _, provider = load_provider(_config(tmp_path, 'kind = "fake"\ntranscript = "run.json"\n'))
    result = run_assistant(
        "q",
        provider=provider,
        session=ToolSession.from_paths(SNAPSHOT),
        provider_kind="fake",
        model="fake",
    )
    assert result.outcome.kind == "answered"


def test_a_fake_without_a_transcript_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, 'kind = "fake"\n')
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(path)
    error = raised.value
    assert error.code == ProviderConfigErrorCode.TRANSCRIPT_MISSING
    assert error.path == path and error.kind == ProviderKind.FAKE
    assert str(error) == "Fake provider configuration names no transcript file."


@pytest.mark.parametrize(
    ("content", "code"),
    [(None, FakeScriptErrorCode.UNREADABLE), ("{not json", FakeScriptErrorCode.INVALID)],
    ids=["missing", "invalid"],
)
def test_an_unusable_transcript_is_refused(tmp_path: Path, content, code) -> None:
    if content is not None:
        (tmp_path / "run.json").write_text(content, encoding="utf-8")
    with pytest.raises(FakeScriptError) as raised:
        load_provider(_config(tmp_path, 'kind = "fake"\ntranscript = "run.json"\n'))
    assert raised.value.code == code
    assert raised.value.path == tmp_path / "run.json"


def test_a_bedrock_profile_that_cannot_be_loaded_fails_before_the_run(tmp_path: Path) -> None:
    path = missing_profile_config(tmp_path)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(path)
    error = raised.value
    assert error.code == ProviderConfigErrorCode.PROFILE_UNAVAILABLE
    assert (error.profile, error.kind, error.path) == (MISSING_PROFILE, ProviderKind.BEDROCK, path)
    assert str(error) == "Configured AWS profile could not be loaded."


def test_no_secret_appears_in_errors_or_providers(tmp_path: Path) -> None:
    config = parse_provider_config(ANTHROPIC)
    variable = ANTHROPIC["api_key_env"]
    with pytest.raises(ProviderConfigError) as raised:
        build_provider(config, config_dir=tmp_path, environ={variable: " "})
    anthropic = build_provider(config, config_dir=tmp_path, environ={variable: SECRET})
    shutil.copy(TRANSCRIPT, tmp_path / "run.json")
    _, fake = load_provider(_config(tmp_path, 'kind = "fake"\ntranscript = "run.json"\n'))
    for value in (raised.value, anthropic, fake):
        for rendering in secret_renderings(value):
            assert SECRET not in rendering
