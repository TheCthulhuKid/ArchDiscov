"""Secret-looking text detection and redaction shared by configuration and transcripts."""

import re

REDACTED = "[REDACTED]"

_TOKEN = r"[A-Za-z0-9_=-]"
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)",
        re.DOTALL,
    ),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    # Opaque tokens: long runs mixing lower case, upper case, and digits. Matching starts
    # only at the beginning of a run, which keeps the scan linear.
    re.compile(
        rf"(?<!{_TOKEN})(?={_TOKEN}*[a-z])(?={_TOKEN}*[A-Z])(?={_TOKEN}*[0-9]){_TOKEN}{{32,}}"
    ),
)


def looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    """Replace every secret-looking span with `[REDACTED]`."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
