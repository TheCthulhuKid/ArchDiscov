"""The bounded file read shared by the JSON readers."""

from pathlib import Path

import pytest

from archdiscov.ingestion.strict_json import FileTooLargeError, read_bounded


def test_a_file_at_the_limit_is_read_and_one_byte_more_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_bytes(b"x" * 10)
    assert read_bounded(path, 10) == b"x" * 10
    with pytest.raises(FileTooLargeError):
        read_bounded(path, 9)


def test_an_unreadable_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_bounded(tmp_path / "missing.json")
