"""Bedrock configuration fields, AWS responses, and signing checks shared by the Bedrock tests."""

from typing import Any

from tests.support.mock_api import AwsResponse

PROFILE = "assistant"
REGION = "eu-west-1"
MODEL = "anthropic.claude-sonnet-5-5-v1:0"
ROLE = "arn:aws:iam::123456789012:role/assistant"
ROLE_KEY = "ASIAROLEKEY000000000"
ROLE_SECRET = "roleSecretKey00000000000000000000000000000"
QUESTION = "Which databases are reachable?"
ANSWER = {
    "output": {"message": {"role": "assistant", "content": [{"text": "None of them."}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
    "metrics": {"latencyMs": 1},
}


def bedrock_fields(**fields: Any) -> dict[str, Any]:
    return {"kind": "bedrock", "model": MODEL, "profile": PROFILE, "region": REGION, **fields}


def assumed_role_response() -> Any:
    """An `AssumeRole` response with the role's credentials."""
    body = (
        '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "<AssumeRoleResult><Credentials>"
        f"<AccessKeyId>{ROLE_KEY}</AccessKeyId><SecretAccessKey>{ROLE_SECRET}</SecretAccessKey>"
        "<SessionToken>roleToken</SessionToken><Expiration>2030-01-01T00:00:00Z</Expiration>"
        "</Credentials></AssumeRoleResult></AssumeRoleResponse>"
    )
    return AwsResponse(200, body.encode())


def signing_key(request: Any) -> str:
    """The access key ID a request was signed with."""
    authorization = request.headers["Authorization"].decode()
    return authorization.split("Credential=")[1].split("/")[0]
