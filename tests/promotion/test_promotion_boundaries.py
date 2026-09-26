"""Import boundaries of the promotion chain (ADR-0005; 01-system.md §3–§4), statically (AST)."""

from __future__ import annotations

import pytest

from tests.test_architecture_boundaries import REPO, _imported_roots, _python_files

#: package → roots it must never import.
FORBIDDEN = {
    # Control Plane storage: Domain only (never research code, never the application layer).
    "infrastructure/registry": {"research", "apps", "strategies", "plugins", "risk"},
    # Production side: never research code (H5), never an execution venue or network.
    "apps/promotion": {"research", "socket", "ssl", "http", "urllib", "requests", "httpx"},
    # Research side: submits to the registry, never reaches into the application plane.
    "research/promotion": {"apps", "strategies"},
}


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_promotion_packages_respect_their_boundaries(package: str) -> None:
    files = _python_files(package)
    assert files, f"{package} does not exist"
    for path in files:
        leaked = _imported_roots(path) & FORBIDDEN[package]
        assert not leaked, f"{path.relative_to(REPO)} imports {sorted(leaked)}"


def test_no_strategy_has_been_placed_in_the_production_packages() -> None:
    """ADR-0005: nothing is promoted today, so ``strategies/`` and ``risk/`` hold no code. When a
    strategy is legitimately promoted (artifact + Equivalence Gate + human review), this test is
    updated in that same reviewed change."""
    assert _python_files("strategies", "risk") == []
