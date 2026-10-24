"""A credential-free, network-free environment for installed-CLI journeys.

The guard is a test-only `sitecustomize` module in a directory that holds nothing else and is
the whole `PYTHONPATH` of the child: the checkout is never importable. It makes every internet
connection, datagram send, and name lookup fail and records the attempt, so a journey can
assert both that the command succeeded and that nothing tried the network. Creating or binding
a local socket (as urllib3 does at import to probe for IPv6) is not network use. `allowing`
opens exactly one loopback port, such as a mock API server's, and nothing else.

`watching` is an opt-in addition for boundary checks: it logs every file the child opens under
the given directories, so a journey can assert that a command read only the files it was
given. Without it the child's environment, and so every other journey, is unchanged.
"""

import os
from pathlib import Path

_HOOK = """\
import os
import socket

_ALLOWED_PORT = os.environ.get("ARCHDISCOV_ALLOWED_LOOPBACK_PORT")


def _refuse(*_args, **_kwargs):
    with open(os.environ["ARCHDISCOV_NETWORK_LOG"], "a", encoding="utf-8") as log:
        log.write("network use attempted\\n")
    raise OSError("network access is disabled for this test")


def _allowed(address):
    \"\"\"Whether the address is the one loopback port this run may reach.\"\"\"
    if _ALLOWED_PORT is None or not isinstance(address, tuple) or len(address) < 2:
        return False
    return address[0] == "127.0.0.1" and str(address[1]) == _ALLOWED_PORT


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto
_original_getaddrinfo = socket.getaddrinfo
_original_create_connection = socket.create_connection


def _guarded(original):
    def wrapper(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _allowed(address):
            _refuse()
        return original(self, address, *args, **kwargs)

    return wrapper


def _sendto(self, *args, **kwargs):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _refuse()
    return _original_sendto(self, *args, **kwargs)


def _getaddrinfo(host, port, *args, **kwargs):
    if not _allowed((host, port)):
        _refuse()
    return _original_getaddrinfo(host, port, *args, **kwargs)


def _create_connection(address, *args, **kwargs):
    if not _allowed(address):
        _refuse()
    return _original_create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded(_original_connect)
socket.socket.connect_ex = _guarded(_original_connect_ex)
socket.socket.sendto = _sendto
socket.getaddrinfo = _getaddrinfo
socket.gethostbyname = _refuse
socket.gethostbyname_ex = _refuse
socket.create_connection = _create_connection

_WATCHED = os.environ.get("ARCHDISCOV_WATCHED_DIRS")
if _WATCHED:
    import sys

    _DIRECTORIES = [os.path.join(item, "") for item in _WATCHED.split(os.pathsep)]
    _FILE_LOG = os.environ["ARCHDISCOV_FILE_LOG"]

    def _audit(event, args):
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = os.path.realpath(os.fsdecode(args[0]))
        # The log itself lives outside the watched directories, so writing it opens nothing
        # that is watched.
        if path.startswith(tuple(_DIRECTORIES)):
            with open(_FILE_LOG, "a", encoding="utf-8") as log:
                log.write(path + "\\n")

    sys.addaudithook(_audit)
"""
_REMOVED_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "OPENAI_", "ANTHROPIC_", "BOTO_")


class OfflineGuard:
    """Environment for child processes plus the log of attempted network use."""

    def __init__(self, directory: Path) -> None:
        self.hook = directory / "offline-hook"
        self.file_log = directory / "file-opens.log"
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

    def allowing(self, port: int, **variables: str) -> dict[str, str]:
        """The environment with one loopback port open, plus extra variables, such as a key."""
        return {**self.env, "ARCHDISCOV_ALLOWED_LOOPBACK_PORT": str(port), **variables}

    def watching(self, *directories: Path, **variables: str) -> dict[str, str]:
        """The environment, plus a log of every file opened under the directories.

        The directories must not contain the guard's own directory or logs.
        """
        watched = [str(directory.resolve()) for directory in directories]
        return {
            **self.env,
            "ARCHDISCOV_WATCHED_DIRS": os.pathsep.join(watched),
            "ARCHDISCOV_FILE_LOG": str(self.file_log),
            **variables,
        }

    @property
    def opened_files(self) -> list[Path]:
        """The distinct files opened under the watched directories, sorted."""
        if not self.file_log.exists():
            return []
        return sorted({Path(line) for line in self.file_log.read_text("utf-8").splitlines()})

    @property
    def attempts(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
