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

from archdiscov.assistant import config, conversation, fake, provider
from archdiscov.assistant.tools import contracts as tool_contracts
from archdiscov.assistant.tools import record as tool_record
from archdiscov.assistant.tools import registry as tool_registry
from archdiscov.assistant.tools import session as tool_session

assert tool_registry.ToolRegistry([]).declarations() == ()
assert tool_session.ToolSession.from_paths(Path({str(SNAPSHOT)!r})).index.resource_ids

assert tool_record.RunRecord().returned() == tool_record.ReturnedIds()
assert tool_contracts.untrusted("x").untrusted_text == "x"

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
