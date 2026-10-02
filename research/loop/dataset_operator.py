"""Finite Dataset-sourced loop operator CLI (ADR-0105 §5; amends ADR-0074 §9).

Usage (the only command; no defaults, no infinite or daemon mode)::

    python -m research.loop.dataset_operator run --config PATH --rounds N \\
        --catalog MODULE:CALLABLE

The parallel of ``research.loop.operator`` for a loop whose rounds read declared Research Dataset
manifests (``research.loop.dataset_compose``) instead of a synthetic market. Everything but the
source is the synthetic operator's own code: the same pre-open checks, the same state / anchor
verification and the same bounded round execution, reports, signal handling and exit codes
(``research.loop.operator``: ``check_code_identity``, ``check_anchor_paths``,
``check_state_dir``, ``run_durable``, ``preparation_failure``). One invocation runs **at most**
``N`` new rounds and exits; continuous running is an external scheduler's job (ADR-0049).

**Before any state is opened** (refusals are exit code 2 unless noted):

1. ``load_dataset_operator_config`` parses and verifies the strict TOML v1 configuration and the
   ADR-0062 freeze registry — **without a frozen Profile every configuration is refused**
   (ADR-0074 §2.8, unchanged; a registry I/O / corruption / lock fault is 5);
2. ``build_dataset_providers`` constructs and verifies the five allowlisted providers;
   ``DatasetOperatorConfig.build`` compiles the one ``DatasetLoopConfig``;
3. ``code_commit`` / ``environment_lock`` must be this repository's clean HEAD and environment;
4. the state and bus anchors are re-checked, and ``state_dir`` must be new or this
   configuration's operator v5 state (its fingerprint: ``dataset_loop_fingerprint`` plus the
   ``operator_identity``);
5. the live ``DatasetCatalog`` (not configuration) comes from exactly one of
   ``--catalog MODULE:CALLABLE`` — a deployment-supplied **trusted** factory resolved like the
   ADR-0095 worker ``--factory`` (``apps.worker.serve.load_factory``), called with no arguments,
   returning a ``DatasetCatalog`` or a context manager yielding one (held open until the loop is
   closed) — or ``main(..., catalog=...)`` from an embedding caller. Without either the command is
   refused before anything is opened: by default it has no side effect. The factory is only
   called after steps 1 – 4 have passed.

**Run.** ``open_dataset_loop(..., operator_identity=...)`` opens the state (v5) with the
configured anchors and its own bus; the rest is ``research.loop.operator``'s **Run**, **Signals**
and **Exit codes**. Nothing here resolves ACTIVE strategies or authorities, writes a lifecycle or
reaches live trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from apps.worker.serve import load_factory
from core.domain.base import canonical_json
from research.loop.compose import llm_content_fingerprint
from research.loop.dataset_compose import dataset_loop_fingerprint, open_dataset_loop
from research.loop.dataset_operator_config import (
    CompiledDatasetOperatorConfig,
    build_dataset_providers,
    load_dataset_operator_config,
)
from research.loop.dataset_source import DatasetCatalog
from research.loop.operator import (
    EXIT_FAULT,
    EXIT_REFUSED,
    OperatorRefused,
    StopRequest,
    _config_path,
    _Once,
    _positive_rounds,
    boundary_stop,
    check_anchor_paths,
    check_code_identity,
    check_state_dir,
    fail,
    preparation_failure,
    run_durable,
)

__all__ = ["CATALOG_ARGUMENT", "main", "run"]

#: The command-line option naming the trusted ``DatasetCatalog`` factory.
CATALOG_ARGUMENT: Final = "--catalog"


class _CatalogUnavailable(RuntimeError):
    """The trusted catalog factory failed while building the catalog (exit code 5)."""


# ---- command line ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.loop.dataset_operator",
        description="Run a bounded batch of the Dataset-sourced research loop (ADR-0105 §5).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="run")
    run_parser = commands.add_parser(
        "run",
        help="run at most N new rounds, then exit",
        description="Run at most N new rounds of the configured loop, then exit.",
        allow_abbrev=False,
    )
    run_parser.add_argument(
        "--config", required=True, type=_config_path, action=_Once, help="operator TOML file"
    )
    run_parser.add_argument(
        "--rounds",
        required=True,
        type=_positive_rounds,
        action=_Once,
        help="maximum number of new rounds in this invocation (positive integer)",
    )
    run_parser.add_argument(
        CATALOG_ARGUMENT,
        action=_Once,
        metavar="MODULE:CALLABLE",
        help="trusted deployment factory returning a DatasetCatalog or a context manager",
    )
    return parser


# ---- preparation ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prepared:
    compiled: CompiledDatasetOperatorConfig
    existing: bool


def _expected_fingerprint(compiled: CompiledDatasetOperatorConfig) -> Mapping[str, Any]:
    """Exactly what ``open_dataset_loop`` writes into / checks against the v5 header (no LLM),
    as plain JSON data."""
    fingerprint = {
        **dataset_loop_fingerprint(compiled.loop_config),
        **llm_content_fingerprint(None),
        "operator_identity": compiled.operator_identity,
    }
    loaded: Mapping[str, Any] = json.loads(canonical_json(fingerprint))
    return loaded


def _prepare(config: Path) -> _Prepared:
    """Everything that must pass before the catalog is built or the state opened (module docs,
    1 – 4)."""
    operator_config = load_dataset_operator_config(config)
    providers = build_dataset_providers(operator_config)
    compiled = operator_config.build(providers)
    if compiled.operator_identity != operator_config.operator_identity:
        raise OperatorRefused("the compiled operator identity is not the configuration's")
    wiring = compiled.loop_config.wiring
    check_code_identity(wiring.code_commit, wiring.environment_lock)
    check_anchor_paths(compiled.paths)
    existing = check_state_dir(compiled.paths, _expected_fingerprint(compiled))
    return _Prepared(compiled, existing)


def _factory_catalog(spec: str, stack: ExitStack) -> DatasetCatalog:
    """Load and call the trusted factory (module docs, 5); its context manager, if it returns
    one, is entered on ``stack``. Only exception type names are reported."""
    try:
        factory = cast(Callable[[], object], load_factory(spec))
    except Exception as exc:  # bad syntax, import / attribute failure, not callable
        raise OperatorRefused(
            f"the catalog factory could not be loaded ({type(exc).__name__})"
        ) from exc
    try:
        produced = factory()
        if isinstance(produced, AbstractContextManager):
            produced = stack.enter_context(produced)
    except Exception as exc:  # a trusted factory that fails (e.g. the catalog does not open)
        raise _CatalogUnavailable(f"the catalog factory failed ({type(exc).__name__})") from exc
    if not isinstance(produced, DatasetCatalog):
        raise OperatorRefused("the catalog factory did not produce a DatasetCatalog")
    return produced


# ---- run ------------------------------------------------------------------------------------


def run(
    config: Path,
    rounds: int,
    stop: StopRequest,
    *,
    catalog: DatasetCatalog | None = None,
    catalog_factory: str | None = None,
) -> int:
    """One bounded batch (module docs): exactly one of ``catalog`` / ``catalog_factory``."""
    if (catalog is None) == (catalog_factory is None):
        return fail(
            EXIT_REFUSED,
            "refused",
            OperatorRefused(
                f"give exactly one of {CATALOG_ARGUMENT} MODULE:CALLABLE or an embedded "
                "DatasetCatalog (the live catalog is not configuration)"
            ),
        )
    if catalog is not None and not isinstance(catalog, DatasetCatalog):
        return fail(EXIT_REFUSED, "refused", OperatorRefused("catalog must be a DatasetCatalog"))
    try:
        prepared = _prepare(config)
    except Exception as exc:  # noqa: BLE001 - classified; nothing was opened
        return preparation_failure(exc)
    compiled = prepared.compiled
    paths = compiled.paths
    with ExitStack() as stack:
        if catalog_factory is not None:
            try:
                catalog = _factory_catalog(catalog_factory, stack)
            except OperatorRefused as exc:
                return fail(EXIT_REFUSED, "refused", exc)
            except _CatalogUnavailable as exc:
                return fail(EXIT_FAULT, "the dataset catalog is unavailable", exc)
        assert catalog is not None
        opened_catalog = catalog
        return run_durable(
            lambda: open_dataset_loop(
                compiled.loop_config,
                state_dir=paths.state_dir,
                catalog=opened_catalog,
                anchor=paths.state_anchor,
                bus_anchor=paths.bus_anchor,
                operator_identity=compiled.operator_identity,
            ),
            loop_id=compiled.loop_config.loop_id,
            operator_identity=compiled.operator_identity,
            paths=paths,
            existing=prepared.existing,
            rounds=rounds,
            stop=stop,
        )


def main(argv: Sequence[str] | None = None, *, catalog: DatasetCatalog | None = None) -> int:
    """Parse the command line (argparse exits 2 on a usage error) and run one bounded batch.

    ``catalog``: supplied by an embedding caller instead of ``--catalog`` (never both)."""
    parser = _parser()
    args = parser.parse_args(None if argv is None else list(argv))
    if catalog is not None and args.catalog is not None:
        parser.error(f"give {CATALOG_ARGUMENT} or an embedded catalog, not both")
    with boundary_stop() as stop:
        return run(args.config, args.rounds, stop, catalog=catalog, catalog_factory=args.catalog)


if __name__ == "__main__":
    sys.exit(main())
