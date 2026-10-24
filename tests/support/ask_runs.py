"""Fake-provider configurations for `ask`, and an `ask` cancelled while its provider hangs."""

import asyncio
import json
from pathlib import Path

import pytest

from archdiscov.assistant.tools.session import ToolSession
from archdiscov.services import ask as ask_module
from archdiscov.services.ask import AskCancelled, AskResult
from tests.paths import FIXTURES_ROOT
from tests.support.scripted_turns import HangingProvider, text_turn
from tests.support.secrets import SECRET

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "network.json"
COMPARISON = FIXTURES_ROOT / "snapshots" / "aws.json"
ASK_QUESTION = f"Which resources are in the network? ({SECRET})"


def fake_config(tmp_path: Path, turns: list[dict], config: str = "") -> Path:
    (tmp_path / "run.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")
    path = tmp_path / "provider.toml"
    path.write_text(f'kind = "fake"\ntranscript = "run.json"\n{config}', encoding="utf-8")
    return path


def network_session() -> ToolSession:
    return ToolSession.from_paths(SNAPSHOT)


def cancelled_ask(tmp_path: Path, monkeypatch, transcript_path: Path | None = None) -> AskResult:
    config_path = fake_config(tmp_path, [text_turn()])
    provider = HangingProvider(answer_first=True)
    built = ask_module.load_provider

    def hanging(path: Path, *, environ=None):
        config, _ = built(path, environ=environ)
        return config, provider

    monkeypatch.setattr(ask_module, "load_provider", hanging)

    async def scenario() -> AskResult:
        task = asyncio.create_task(
            ask_module.ask_question_async(
                ASK_QUESTION,
                snapshot_path=SNAPSHOT,
                config_path=config_path,
                transcript_path=transcript_path,
            )
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(AskCancelled) as raised:
            await task
        return raised.value.result

    return asyncio.run(scenario())
