"""A credential-free, network-free environment for installed-CLI journeys.

The guard is a test-only `sitecustomize` module in a directory that holds nothing else and is
the whole `PYTHONPATH` of the child: the checkout is never importable. It makes every internet
connection, datagram send, and name lookup fail and records the attempt, so a journey can
assert both that the command succeeded and that nothing tried the network. Creating or binding
a local socket (as urllib3 does at import to probe for IPv6) is not network use.
"""

import os
from pathlib import Path

_HOOK = """\
import os
import socket


def _refuse(*_args, **_kwargs):
    with open(os.environ["ARCHDISCOV_NETWORK_LOG"], "a", encoding="utf-8") as log:
        log.write("network use attempted\\n")
    raise OSError("network access is disabled for this test")


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto


def _guarded(original):
    def wrapper(self, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse()
        return original(self, *args, **kwargs)

    return wrapper


socket.socket.connect = _guarded(_original_connect)
socket.socket.connect_ex = _guarded(_original_connect_ex)
socket.socket.sendto = _guarded(_original_sendto)
socket.getaddrinfo = _refuse
socket.gethostbyname = _refuse
socket.gethostbyname_ex = _refuse
socket.create_connection = _refuse
"""
_REMOVED_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "OPENAI_", "ANTHROPIC_", "BOTO_")


class OfflineGuard:
    """Environment for child processes plus the log of attempted network use."""

    def __init__(self, directory: Path) -> None:
        self.hook = directory / "offline-hook"
        self.hook.mkdir(parents=True)
        (self.hook / "sitecustomize.py").write_text(_HOOK, encoding="utf-8")
        self.log = directory / "network.log"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH" and not key.startswith(_REMOVED_PREFIXES)
        }
        self.env.update(
            PYTHONPATH=str(self.hook),
            ARCHDISCOV_NETWORK_LOG=str(self.log),
            AWS_EC2_METADATA_DISABLED="true",
            AWS_SHARED_CREDENTIALS_FILE=str(directory / "no-aws-credentials"),
            AWS_CONFIG_FILE=str(directory / "no-aws-config"),
            NO_COLOR="1",
            COLUMNS="200",
        )

    @property
    def attempts(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
