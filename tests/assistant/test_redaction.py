"""Secret-looking text: which credential formats are detected and redacted, and which ordinary
values are left alone.

Free-text redaction is the last defence for transcripts, rendered narrative, and answer
output, after ingestion has already redacted sensitive fields by name. Each format below is
one a credential commonly takes; adding a pattern means adding its rows here.
"""

import time

import pytest

from archdiscov.assistant.redaction import REDACTED, looks_secret, redact_secrets

SECRETS = {
    "anthropic or openai key": "sk-ant-api03-abcdefghijklmnop0123456789",
    "aws access key id": "AKIA" + "IOSFODNN7EXAMPLE",
    "aws secret access key with slashes": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "bearer token": "Bearer abc.def-ghi",
    "github token": "ghp_" + "aB3" * 12,
    "slack token": "xo" + "xb-123456789012-123456789012-abcdefghijklmnopqrstuvwx",
    "json web token": (
        "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    ),
    "private key": "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
}

# Secrets inside other text: what must disappear, and what must remain around it.
IN_CONTEXT = [
    (
        "postgres://admin:hunter2pass@db.example.com:5432/app",
        "hunter2pass",
        f"postgres://{REDACTED}@db.example.com:5432/app",
    ),
    (
        "DefaultEndpointsProtocol=https;AccountName=store;AccountKey=abcd/EFGH+ijkl/MNOP==;"
        "EndpointSuffix=core.windows.net",
        "abcd/EFGH+ijkl/MNOP==",
        f"DefaultEndpointsProtocol=https;AccountName=store;{REDACTED};"
        "EndpointSuffix=core.windows.net",
    ),
    (
        "The key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY was found.",
        "wJalrXUtnFEMI/K7MDENG",
        f"The key {REDACTED} was found.",
    ),
]

ORDINARY = {
    "resource id": "123456789012/eu-west-1/ec2/physical/vpc-0abc1234def567890",
    "finding id": "12d1701f7095830496983615ee9ee03cc0ab1532644e7e4710301e2a97065b25",
    "database url without credentials": "postgres://db.example.com:5432/app",
    "password policy": "The password policy requires 12 characters.",
    "lower-case path of forty characters": "reports/quarterly/2026/summary/region/eu",
}


@pytest.mark.parametrize("secret", list(SECRETS.values()), ids=list(SECRETS))
def test_each_credential_format_is_detected_and_redacted(secret: str) -> None:
    assert looks_secret(secret)
    redacted = redact_secrets(f"value: {secret} end")
    assert secret not in redacted and REDACTED in redacted
    assert not looks_secret(redacted)


@pytest.mark.parametrize(("text", "secret", "expected"), IN_CONTEXT)
def test_a_secret_in_context_is_redacted_and_its_context_kept(
    text: str, secret: str, expected: str
) -> None:
    assert looks_secret(text)
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert redacted == expected


@pytest.mark.parametrize("text", list(ORDINARY.values()), ids=list(ORDINARY))
def test_ordinary_values_are_left_alone(text: str) -> None:
    assert not looks_secret(text)
    assert redact_secrets(text) == text


def test_detection_stays_linear_on_adversarial_input() -> None:
    # A base64 run far longer than a secret key, which the key pattern must not rescan.
    text = "aB3/" * 50_000
    started = time.monotonic()
    looks_secret(text)
    redact_secrets(text)
    assert time.monotonic() - started < 2
