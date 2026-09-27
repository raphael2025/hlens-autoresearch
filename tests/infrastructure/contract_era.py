"""Write persisted Phase 1 data as an earlier published contract version would have (test only).

``written_at("2.0.0")`` stands for the code before the 2.1.0 bump (ADR-0052 versioned replay):
every write group started inside it is stamped 2.0.0 — the single new-group version source
(``infrastructure.contract_version.new_group_version``) answers 2.0.0 — and every contract object
constructed without an explicit envelope is 2.0.0 (``contract_schema_version_scope``), exactly
what the 2.0.0 code built. Leaving the block restores the current version. Only the test replaces
the new-group source; production code never writes a new group inside a scope (it fails closed).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from core.domain.base import PUBLISHED_CONTRACT_SCHEMA_VERSIONS, contract_schema_version_scope
from infrastructure import contract_version

__all__ = ["written_at"]


@contextmanager
def written_at(version: str) -> Iterator[str]:
    if version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:  # pragma: no cover - test misuse
        raise ValueError(f"{version} is not a published contract version")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(contract_version, "new_group_version", lambda: version)
        with contract_schema_version_scope(version):
            yield version
