"""Small markdown readers shared by the documentation tests."""

import re
from pathlib import Path

from tests.paths import PROJECT_ROOT

DOC_FILES = [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md"))]
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def prose(path: Path) -> str:
    """The document without fenced code blocks."""
    return _FENCE.sub("", path.read_text(encoding="utf-8"))


def code_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [block.split("\n", 1)[1].rsplit("```", 1)[0] for block in _FENCE.findall(text)]


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: dict[str, int] = {}
    result = set()
    for line in prose(path).splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            base = slug(match.group(1))
            count = found.get(base, 0)
            found[base] = count + 1
            result.add(base if count == 0 else f"{base}-{count}")
    return result


def table_rows(path: Path, header_start: str) -> list[list[str]]:
    """Cells of the data rows of the table whose header row starts with `header_start`."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(header_start):
            rows = []
            for row in lines[index + 2 :]:
                if not row.startswith("|"):
                    break
                rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
            return rows
    raise AssertionError(f"no table starting {header_start!r} in {path.name}")
