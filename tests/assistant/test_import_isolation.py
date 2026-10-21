import subprocess
import sys

from tests.paths import PROJECT_ROOT, TESTS_ROOT

VENDOR_MODULES = ("anthropic", "openai", "boto3", "botocore")
TRANSCRIPT = TESTS_ROOT / "fixtures" / "assistant" / "multi_step.json"
SNAPSHOT = TESTS_ROOT / "fixtures" / "snapshots" / "aws.json"

# Imports would fail loudly if any assistant module reached a vendor SDK; the transcript
# replay exercises the modules, not just their top-level imports.
SCRIPT = f"""
import sys
from importlib.abc import MetaPathFinder

BLOCKED = {VENDOR_MODULES!r}


class Blocker(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked vendor SDK: " + name)
        return None


sys.meta_path.insert(0, Blocker())

from pathlib import Path

from archdiscov.assistant import config, conversation, fake, instructions, orchestrator, provider
from archdiscov.assistant import redaction, runs
from archdiscov.assistant.tools import contracts as tool_contracts
from archdiscov.assistant.tools import session as tool_session
from archdiscov.assistant.tools.catalog import ALL_TOOLS, default_registry

# Every tool runs once, so lazily imported service code is exercised under the blocker.
registry = default_registry()
session = tool_session.ToolSession.from_paths(Path({str(SNAPSHOT)!r}))
resource = session.index.resource_ids[0]
arguments = {{
    "inspect_resource": {{"resource_id": resource}},
    "get_neighbours": {{"resource_id": resource}},
    "find_paths": {{"resource_id": resource}},
    "get_evidence": {{"evidence_id": "missing"}},
    "run_analyzer": {{"kind": "ownership"}},
    "get_saved_analysis": {{"number": 1}},
    "get_saved_comparison": {{"number": 1}},
}}
for tool in ALL_TOOLS:
    result = registry.dispatch(
        session,
        conversation.ToolCall(
            id="call",
            name=tool.name,
            arguments=conversation.ParsedToolArguments(value=arguments.get(tool.name, {{}})),
        ),
    )
    assert '"tool_internal_error"' not in result.content, tool.name
assert session.record.returned().snapshots
assert tool_contracts.untrusted("x").untrusted_text == "x"
assert runs.RunLimits.from_config(config.FakeProviderConfig()) == runs.RunLimits()
assert redaction.redact_secrets("Bearer abc") == redaction.REDACTED
assert len(instructions.initial_conversation("q").messages) == 2
reply = {{"message": {{"role": "assistant", "text": "a"}}, "stop_reason": "end"}}
answer = fake.FakeTranscript.model_validate({{"turns": [{{"kind": "respond", "response": reply}}]}})
run = orchestrator.run_assistant(
    "q", provider=fake.FakeProvider(answer), session=session, provider_kind="fake", model="fake"
)
assert run.outcome.kind == "answered"

transcript = fake.load_fake_transcript(Path({str(TRANSCRIPT)!r}))
replayed = fake.FakeProvider(transcript)
request = provider.CompletionRequest(
    conversation=conversation.Conversation(messages=(conversation.UserMessage(text="q"),)),
    tools=tuple(
        conversation.ToolDeclaration(name=name, description=name, input_schema={{"type": "object"}})
        for name in ("search_resources", "inspect_resource")
    ),
)
assert provider.complete(replayed, request).stop_reason == provider.StopReason.TOOL_USE
assert config.parse_provider_config({{"kind": "fake"}}).kind == config.ProviderKind.FAKE
print(",".join(module for module in BLOCKED if module in sys.modules))
"""


def test_assistant_modules_work_with_vendor_sdks_blocked() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""
