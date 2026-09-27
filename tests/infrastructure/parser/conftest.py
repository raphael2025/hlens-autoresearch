"""Every D1 parse in these tests is also run through the disk spool (E1-CAP-1).

``spool_archive`` must give exactly ``parse_archive``'s verdict for every input the parser tests
build — hostile ZIPs, grammar and cross-row refusals, integrity mismatches, valid archives — so
each test module that calls ``parse_archive_bytes`` has it wrapped: the in-memory outcome is
returned unchanged, after the spooled outcome of the same bytes was checked against it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.infrastructure.parser.parser_support import assert_spool_agrees


@pytest.fixture(autouse=True)
def _spool_agrees_with_every_parse(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    module = request.module
    original: Callable[..., Any] | None = getattr(module, "parse_archive_bytes", None)
    if original is None:
        return
    directory: Path = tmp_path_factory.mktemp("spool")

    def both(parse_request: Any, data: bytes) -> Any:
        outcome = original(parse_request, data)
        assert_spool_agrees(parse_request, data, directory)
        return outcome

    monkeypatch.setattr(module, "parse_archive_bytes", both)
