"""Repository hygiene the D2 write path must keep true (roadmap Phase 1 #21).

The store publishes archive bytes to a local ``file://`` warehouse and writes revisions to
Iceberg. Neither may ever end up in Git, and no credential may either. These checks run over
``git ls-files``, so they see exactly what a push would carry.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

#: Market-data / warehouse artefacts that must never be tracked.
DATA_SUFFIXES = {".zip", ".parquet", ".avro", ".orc", ".sqlite", ".db"}
#: Credential-shaped file names.
SECRET_NAMES = re.compile(r"(^|/)(\.env(\.[a-z0-9_-]+)?|[^/]*\.(pem|key|p12|pfx))$")
#: A PostgreSQL DSN carrying a password.
DSN_WITH_PASSWORD = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9]+)?://[^\s:@/]+:([^\s@/]+)@")
#: Other credential shapes.
SECRET_CONTENT = re.compile(
    r"(api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9/+_-]{16,})|(-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
#: A DSN password is only acceptable when it is obviously not one: a placeholder, a format
#: interpolation (``{password}``) or a fake used by a unit test.
PLACEHOLDER = re.compile(
    r"^(\{[a-z_.]+\}|p|change_me[a-z0-9_]*|fake[a-z0-9_-]*|test[a-z0-9_-]*|"
    r"placeholder|example|[a-z_]*not_a_real[a-z_]*)$",
    re.IGNORECASE,
)
#: The only file tracked under ``data/`` (documented mount point, no data).
DATA_README = "data/README.md"


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [name for name in result.stdout.split("\0") if name]


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    return tracked_files()


def test_no_market_data_or_warehouse_artefacts_are_tracked(tracked: list[str]) -> None:
    offenders = [name for name in tracked if Path(name).suffix.lower() in DATA_SUFFIXES]
    assert offenders == []


def test_the_local_data_directory_holds_nothing_but_its_readme(tracked: list[str]) -> None:
    assert [name for name in tracked if name.startswith("data/")] == [DATA_README]


def test_no_credential_shaped_files_are_tracked(tracked: list[str]) -> None:
    offenders = [name for name in tracked if SECRET_NAMES.search(name)]
    assert offenders == [".env.example"], offenders


def test_no_tracked_text_file_carries_a_credential(tracked: list[str]) -> None:
    offenders: list[str] = []
    for name in tracked:
        path = REPO / name
        if not path.is_file() or path.suffix in {".png", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if SECRET_CONTENT.search(text):
            offenders.append(name)
        for password in DSN_WITH_PASSWORD.findall(text):
            if PLACEHOLDER.fullmatch(password) is None:
                offenders.append(f"{name}: DSN password {password!r}")
    assert offenders == []


def test_the_env_example_holds_placeholders_only(tracked: list[str]) -> None:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    dsn_line = text.split("HLENS_CATALOG_URI", 1)[-1].splitlines()[0]
    password = DSN_WITH_PASSWORD.search(dsn_line)
    assert password is not None and PLACEHOLDER.fullmatch(password.group(1)) is not None
    assert not SECRET_CONTENT.search(text)
