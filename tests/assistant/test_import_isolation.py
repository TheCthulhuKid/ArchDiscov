import subprocess
import sys

from tests.paths import PROJECT_ROOT, TESTS_ROOT

VENDOR_MODULES = ("anthropic", "openai", "boto3", "botocore")
TRANSCRIPT = TESTS_ROOT / "fixtures" / "assistant" / "multi_step.json"
SNAPSHOT = TESTS_ROOT / "fixtures" / "snapshots" / "aws.json"

# The answer output is imported first, before anything loads the CLI or Typer.
ANSWER_OUTPUT_SCRIPT = """
import sys

from archdiscov.assistant import answer_output

loaded = [
    name for name in sys.modules if name == "typer" or name.startswith(("typer.", "archdiscov.cli"))
]
assert not loaded, loaded
"""

# Imports would fail loudly if any assistant module reached a vendor SDK; the transcript
# replay exercises the modules, not just their top-level imports.
ASSISTANT_SCRIPT = f"""
from importlib.abc import MetaPathFinder

BLOCKED = {VENDOR_MODULES!r}


class Blocker(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked vendor SDK: " + name)
        return None


blocker = Blocker()
sys.meta_path.insert(0, blocker)

from pathlib import Path

from archdiscov.assistant import config, conversation, fake, instructions, orchestrator, provider
from archdiscov.assistant import answer_rendering, answer_results, answer_validation, answers
from archdiscov.assistant import adapters, answer_service, providers, redaction, repair
from archdiscov.assistant import rejections, runs
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
assert adapters.retry_wait(ValueError(), 0, 60) == adapters.RETRY_BACKOFF_SECONDS
assert len(instructions.initial_conversation("q").messages) == 2
assert answers.submit_answer_declaration().name == "submit_answer"
assert len(rejections.RejectionCode) == len(rejections.REJECTION_MESSAGES)
reply = {{"message": {{"role": "assistant", "text": "a"}}, "stop_reason": "end"}}
answer = fake.FakeTranscript.model_validate({{"turns": [{{"kind": "respond", "response": reply}}]}})
run = orchestrator.run_assistant(
    "q", provider=fake.FakeProvider(answer), session=session, provider_kind="fake", model="fake"
)
assert run.outcome.kind == "answered"
proposal = answers.AnswerProposal.model_validate(
    {{
        "status": "insufficient_evidence",
        "contexts": [{{"side": "primary", "source_kind": session.index.source_kind.value}}],
        "insufficient_reason": "no_applicable_findings",
        "summary": "Nothing applicable.",
    }}
)
assert answer_validation.validate_proposal(proposal, session) == ()
rendered = answer_rendering.render_answer(
    proposal, session, answer_results.AnswerOutcome(status="answered")
)
assert rendered.narrative.summary == "Nothing applicable."
failed = answer_results.AnswerOutcome(status="failed", failure="cancelled")
assert answer_rendering.render_failure(session, failed).failure.reason == "cancelled"
assert repair.NO_PROPOSAL
twice = fake.FakeTranscript.model_validate(
    {{"turns": [{{"kind": "respond", "response": reply}}] * 2}}
)
answered = answer_service.answer_question(
    "q",
    provider=fake.FakeProvider(twice),
    session=tool_session.ToolSession.from_paths(Path({str(SNAPSHOT)!r})),
    provider_kind="fake",
    model="fake",
)
assert answered.outcome.failure == "no_proposal_after_repair"
from archdiscov.assistant import answer_output

assert answer_output.answer_text(answered.rendered).startswith("Answer: failed")
assert answer_output.answer_data(answered.rendered)["exit_code"] == 5
assert answer_results.final_outcome(
    run.outcome, (), repaired=True
).failure == answer_results.FailureReason.NO_PROPOSAL_AFTER_REPAIR
import asyncio

run = asyncio.run(
    orchestrator.run_assistant_async(
        "q", provider=fake.FakeProvider(answer), session=session, provider_kind="fake", model="fake"
    )
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
built = providers.build_provider(
    config.parse_provider_config({{"kind": "fake", "transcript": {str(TRANSCRIPT)!r}}}),
    config_dir=Path("."),
)
assert built.remaining_turns > 0
# Only building a vendor adapter imports its SDK.
vendor_configs = {{
    "anthropic": {{"kind": "anthropic", "model": "m", "api_key_env": "ASK_KEY"}},
    "openai": {{
        "kind": "openai_compatible",
        "model": "m",
        "api_key_env": "ASK_KEY",
        "endpoint": "https://api.example.com/v1",
        "protocol": "responses",
    }},
    "boto3": {{"kind": "bedrock", "model": "m", "profile": "assistant", "region": "eu-west-1"}},
}}
for sdk, data in vendor_configs.items():
    vendor = config.parse_provider_config(data)
    try:
        providers.build_provider(vendor, config_dir=Path("."), environ={{"ASK_KEY": "k"}})
    except ImportError as error:
        assert "blocked vendor SDK: " + sdk in str(error), error
    else:
        raise AssertionError("an adapter was built without its SDK: " + sdk)
import json
import tempfile

from archdiscov.services import ask

with tempfile.TemporaryDirectory() as directory:
    settings = Path(directory) / "provider.toml"
    script = Path(directory) / "run.json"
    script.write_text(json.dumps({{"turns": [{{"kind": "respond", "response": reply}}] * 2}}))
    settings.write_text("kind = 'fake'" + chr(10) + "transcript = 'run.json'", encoding="utf-8")
    asked = ask.ask_question("q", snapshot_path=Path({str(SNAPSHOT)!r}), config_path=settings)
assert asked.summary.provider_kind == "fake"
print(",".join(module for module in BLOCKED if module in sys.modules))
# The CLI loads the AWS SDK; from here on only `mcp` is watched.
sys.meta_path.remove(blocker)
"""

# Every command module, the `mcp` command's help and service module, an `ask` run, and an
# evaluation leave `mcp` unimported; only building the MCP server imports it.
MCP_SCRIPT = f"""
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.services.evaluation import evaluate_suite
from tests.support.evaluation_suites import README_SUITE, write_fake_config

from archdiscov.assistant import questions

assert len(questions.README_QUESTIONS) == 5
default_registry()
assert CliRunner().invoke(app, ["--help"]).exit_code == 0
# The `mcp` command and its service load `mcp` only when serving.
assert CliRunner().invoke(app, ["mcp", "--help"]).exit_code == 0
from archdiscov.services import mcp as mcp_service

assert mcp_service.serve_selection
example = README_SUITE.parent / "ownership"
asked = CliRunner().invoke(
    app,
    [
        "ask",
        "Which resources appear to have no clear owner?",
        "--snapshot",
        str(example / "snapshot.json"),
        "--config",
        str(example / "provider.toml"),
    ],
)
assert asked.exit_code == 0, asked.output
with tempfile.TemporaryDirectory() as directory:
    config = write_fake_config(Path(directory) / "fake.toml")
    report = evaluate_suite(README_SUITE, [config])
assert report.providers[0].totals.failed == 0
assert not any(name == "mcp" or name.startswith("mcp.") for name in sys.modules)

from archdiscov.assistant.mcp_server import build_server
from archdiscov.assistant.tools.session import ToolSession

build_server(ToolSession.from_paths(Path({str(SNAPSHOT)!r})))
assert "mcp" in sys.modules
"""


def test_assistant_modules_isolate_vendor_sdks_mcp_and_the_cli() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", ANSWER_OUTPUT_SCRIPT + ASSISTANT_SCRIPT + MCP_SCRIPT],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""
