"""Run a State over persisted feature results from the command line (ADR-0102; Phase 2).

``python -m research.states.run_cli compute`` runs ``run_state`` over persisted ``FeatureRequest`` /
``FeatureResult`` JSON pairs, an explicit ``StateSpec`` JSON and a built-in provider reference
(``name@version``). By default it only prints a summary. ``--store PATH`` additionally writes the
run to a ``StateResultStore``; ``--apply-table`` additionally appends it to the existing
``state.states`` table (settings and catalog are read only then; the table is never created here —
use ``infrastructure.state.create_state_tables``). Reading back and listing stored runs is
``infrastructure.state.run_cli`` (``show`` / ``list``).

This lives on the research side because providers are resolved from ``plugins.states`` by plain
imports (``infrastructure`` must not depend on ``plugins``; ADR-0087: entry points load manifests
only). Research only, never production (H5).

A ``FeatureResult`` does not name its feature, so each input is a request / result pair
(``--feature REQUEST.json RESULT.json``, repeatable): ``state_inputs`` binds the request's feature
reference and checks the result answers it. All pairs must share the same evaluation times. A
trained spec (``training_window`` set) without a ``seed`` is refused before anything runs
(ADR-0035).

Errors never print settings, DSNs or exception arguments of unexpected failures: only the failing
step and the exception type are written to stderr.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Final

from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateProvider, UnsupportedState
from core.domain.specs import StateSpec
from infrastructure.catalog.definitions import TableDefinitionRegistry
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.settings import Settings
from infrastructure.state.iceberg import StateTable
from infrastructure.state.run_cli import CliError, read_contract, summary_lines
from infrastructure.state.runner import StateRunnerError, run_state, state_inputs, state_request
from infrastructure.state.store import StateResultStore, StateStoreCorrupted
from infrastructure.state.table_definition import PHASE2_TABLES, STATE_STATES
from plugins.states import (
    LiquidityRegimeProvider,
    ReturnShockProvider,
    TrendRangeProvider,
    VolatilityRegimeProvider,
    VolatilitySqueezeProvider,
)

__all__ = ["PROVIDERS", "main"]

#: Built-in ``StateProvider`` name -> class (checked against the built-in state manifests in tests).
PROVIDERS: Final[dict[str, type]] = {
    "volatility_regime": VolatilityRegimeProvider,
    "liquidity_regime": LiquidityRegimeProvider,
    "trend_range": TrendRangeProvider,
    "volatility_squeeze": VolatilitySqueezeProvider,
    "return_shock": ReturnShockProvider,
}


def _provider_for(reference: str, spec: StateSpec) -> StateProvider:
    name, separator, version = reference.partition("@")
    cls = PROVIDERS.get(name)
    if cls is None or not separator or not version:
        known = ", ".join(sorted(PROVIDERS))
        raise CliError(f"unknown provider {reference!r}: expected name@version, name in [{known}]")
    try:
        provider: StateProvider = cls([spec])
    except (ValueError, TypeError) as exc:
        raise CliError(f"{spec.ref} cannot be served by {name}: {exc}") from None
    if provider.descriptor.plugin_key != reference:
        raise CliError(
            f"provider {reference} is not available (built-in: {provider.descriptor.plugin_key})"
        )
    return provider


def _compute(args: argparse.Namespace) -> int:
    spec = read_contract(StateSpec, args.spec, "state spec")
    if spec.training_window is not None and spec.seed is None:
        raise CliError(
            f"{spec.ref} is trained (training_window set) but does not fix its seed (ADR-0035)"
        )
    pairs: list[tuple[FeatureRequest, FeatureResult]] = []
    for request_path, result_path in args.feature:
        pairs.append(
            (
                read_contract(FeatureRequest, Path(request_path), "feature request"),
                read_contract(FeatureResult, Path(result_path), "feature result"),
            )
        )
    times = {request.evaluation_times for request, _ in pairs}
    if len(times) != 1:
        raise CliError("all feature requests must share the same evaluation times")
    evaluation_times = next(iter(times))
    provider = _provider_for(args.provider, spec)
    request = state_request(spec, evaluation_times, state_inputs(pairs))
    result = run_state(provider, spec, request)

    for line in summary_lines(result):
        print(line)
    if args.store is not None:
        path = StateResultStore(args.store).put(result)
        print(f"stored\t{path}")
    if args.apply_table:
        with ExitStack() as stack:
            settings = Settings()  # type: ignore[call-arg]
            registry = TableDefinitionRegistry(PHASE1_TABLES + PHASE2_TABLES)
            adapter = stack.enter_context(open_postgres_catalog_adapter(settings, registry))
            written = StateTable(adapter).write(spec, request, result, provider.descriptor)
        status = "already present (verified)" if written.replayed else "appended"
        print(
            f"table\t{STATE_STATES.table}\trows={written.row_count}"
            f"\tsnapshot={written.snapshot_id}\t{status}"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a State over feature results (ADR-0102). Inert unless asked."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compute = commands.add_parser(
        "compute", help="run a StateSpec over feature results; print a summary by default"
    )
    compute.add_argument("--spec", type=Path, required=True, help="StateSpec JSON file")
    compute.add_argument(
        "--provider",
        required=True,
        help="built-in provider as name@version, e.g. volatility_regime@1.0.0",
    )
    compute.add_argument(
        "--feature",
        nargs=2,
        action="append",
        required=True,
        metavar=("REQUEST_JSON", "RESULT_JSON"),
        help="a FeatureRequest / FeatureResult JSON pair (repeatable)",
    )
    compute.add_argument("--store", type=Path, help="also write the run to this StateResultStore")
    compute.add_argument(
        "--apply-table",
        action="store_true",
        help="also append the run to state.states (reads settings, opens the PostgreSQL catalog)",
    )
    compute.set_defaults(handler=_compute)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (CliError, StateRunnerError, UnsupportedState, StateStoreCorrupted) as exc:
        print(f"state {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Settings / catalog / storage errors can embed DSNs or paths: name the type only.
        print(f"state {args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
