"""Secret-looking text detection and redaction shared by configuration and transcripts."""

import re

REDACTED = "[REDACTED]"

_TOKEN = r"[A-Za-z0-9_=-]"
_BASE64 = r"[A-Za-z0-9/+]"
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)",
        re.DOTALL,
    ),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    # JSON Web Tokens: a header, a payload, and a signature, each base64url.
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # The password in a URL's user information (`scheme://user:password@host`).
    re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+(?=@)"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    # Credentials in connection strings, such as Azure storage's `AccountKey=...`.
    re.compile(
        r"\b(?:AccountKey|SharedAccessKey|SharedAccessSignature|Password|Pwd)=[^;\s\"']+",
        re.IGNORECASE,
    ),
    # AWS secret access keys: exactly 40 base64 characters mixing cases and digits. Those
    # without `/` or `+` are opaque tokens below; these need one, so ordinary words and
    # paths without both cases and digits are left alone. The lookaheads see only the 40
    # characters, and matching starts only at the beginning of a run, so the scan is linear.
    re.compile(
        rf"(?<![A-Za-z0-9/+=])(?={_BASE64}{{0,39}}[/+])(?={_BASE64}{{0,39}}[a-z])"
        rf"(?={_BASE64}{{0,39}}[A-Z])(?={_BASE64}{{0,39}}[0-9]){_BASE64}{{40}}"
        r"(?![A-Za-z0-9/+=])"
    ),
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
