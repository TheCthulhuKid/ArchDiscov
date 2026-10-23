"""AWS session construction shared by collection and the Bedrock adapter; it imports no SDK.

Both start from a named profile session made by a caller-supplied factory (such as
`boto3.Session`) and may then assume a role named by ARN and session name. Each caller calls
STS itself and maps failures to its own errors; this module only shapes the request and
checks the returned credentials.
"""

from collections.abc import Callable
from typing import Any

_CREDENTIAL_KEYS = ("AccessKeyId", "SecretAccessKey", "SessionToken")


def profile_session(factory: Callable[..., Any], profile: str | None, region: str | None) -> Any:
    """A session for `profile` (the SDK's default when None), in `region` when given."""
    return factory(profile_name=profile, **({"region_name": region} if region else {}))


def assume_role_parameters(
    role_arn: str, session_name: str, external_id: str | None = None
) -> dict[str, str]:
    """The `AssumeRole` request for a role and session name."""
    params = {"RoleArn": role_arn, "RoleSessionName": session_name}
    if external_id is not None:
        params["ExternalId"] = external_id
    return params


def role_credentials(response: Any) -> dict[str, Any] | None:
    """The `AssumeRole` response's credentials, or None when any key is missing or blank."""
    credentials = response.get("Credentials") if isinstance(response, dict) else None
    if not isinstance(credentials, dict) or any(
        not isinstance(credentials.get(key), str) or not credentials[key].strip()
        for key in _CREDENTIAL_KEYS
    ):
        return None
    return credentials
