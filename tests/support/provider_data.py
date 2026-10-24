"""Provider configurations, tool declarations, and turns shared by the provider tests."""

from pathlib import Path

from archdiscov.assistant.conversation import ToolDeclaration

ANTHROPIC = {"kind": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ANTHROPIC_API_KEY"}
OPENAI = {
    "kind": "openai_compatible",
    "model": "gpt-5",
    "api_key_env": "OPENAI_API_KEY",
    "endpoint": "https://api.openai.com/v1",
    "protocol": "responses",
}
BEDROCK = {
    "kind": "bedrock",
    "model": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "profile": "assistant",
    "region": "eu-west-1",
}
# Long and opaque, as real signatures are: the secret checks would reject it as text.
SIGNATURE = "EqQBCkgIBhABGAIiQL" + "x7Kp2QmZ9vR4tW8yB1nC6dF3gH5jL0aS" * 12
THINKING = {"type": "thinking", "thinking": "", "signature": SIGNATURE}
SEARCH_TOOL = ToolDeclaration(
    name="search_resources",
    description="Search recorded resources.",
    input_schema={"type": "object", "properties": {"type": {"type": "string"}}},
)
INSPECT_TOOL = ToolDeclaration(
    name="inspect_resource", description="Inspect one resource.", input_schema={"type": "object"}
)
EXCHANGE = (
    {
        "role": "assistant",
        "text": "Searching.",
        "tool_calls": [
            {
                "id": "call_0",
                "name": "search_resources",
                "arguments": {"kind": "parsed", "value": {"type": "rds_instance"}},
            },
            {
                "id": "call_1",
                "name": "inspect_resource",
                "arguments": {"kind": "malformed", "reason": "invalid_json"},
            },
        ],
    },
    {
        "role": "tool",
        "results": [
            {"call_id": "call_0", "content": "[]"},
            {"call_id": "call_1", "content": "Arguments were not JSON.", "is_error": True},
        ],
    },
    {"role": "user", "text": "Submit your answer now."},
)
# No AWS configuration is readable in tests, so this profile never exists.
MISSING_PROFILE = "archdiscov-missing-profile"


def missing_profile_config(directory: Path) -> Path:
    """A Bedrock configuration file naming a profile that cannot be loaded."""
    lines = (f'{key} = "{value}"' for key, value in {**BEDROCK, "profile": MISSING_PROFILE}.items())
    path = directory / "provider.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
