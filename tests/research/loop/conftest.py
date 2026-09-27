"""Shared loop-test hygiene.

A durable loop holds its state directory's single-writer lock (``state.lock``)
until the loop object is released. Loop objects contain reference cycles, so a loop dropped at the
end of a test is only freed by the cycle collector; collecting after every test releases every
dropped writer before the next test opens the same (often module-scoped) directory. Assertions are
unaffected: a test that needs two writers alive at once still gets ``LoopStateLocked``.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _release_dropped_loops() -> Iterator[None]:
    yield
    gc.collect()
