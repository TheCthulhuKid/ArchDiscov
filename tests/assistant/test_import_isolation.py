import subprocess
import sys

import pytest

from tests.paths import PROJECT_ROOT

VENDOR_MODULES = ("anthropic", "openai", "boto3", "botocore")


@pytest.mark.parametrize(
    "module", ["archdiscov.assistant.conversation", "archdiscov.assistant.provider"]
)
def test_assistant_contracts_import_no_vendor_sdk(module: str) -> None:
    script = (
        "import importlib, sys\n"
        f"importlib.import_module({module!r})\n"
        f"print(','.join(m for m in {VENDOR_MODULES!r} if m in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    assert completed.stdout.strip() == ""
