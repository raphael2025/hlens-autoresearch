"""Run, read back and list State runs from the command line (ADR-0102; Phase 2).

``python -m infrastructure.state.run_cli`` has three sub-commands, all inert unless asked:

- ``compute``: ``run_state`` over persisted ``FeatureRequest`` / ``FeatureResult`` JSON pairs, an
  explicit ``StateSpec`` JSON and a built-in provider reference (``name@version``). By default it
  only prints a summary. ``--store PATH`` additionally writes the run to a ``StateResultStore``;
  ``--apply-table`` additionally appends it to the existing ``state.states`` table (settings and
  catalog are read only then; the table is never created here — use ``create_state_tables``).
- ``show``: read one run back from a store by ``result_hash`` (content and canonical form verified).
- ``list``: the ``result_hash`` of every run in a store.

A ``FeatureResult`` does not name its feature, so each input is a request / result pair
(``--feature REQUEST.json RESULT.json``, repeatable): ``state_inputs`` binds the request's feature
reference and checks the result answers it. All pairs must share the same evaluation times.

Providers are resolved from a fixed table of dotted paths and imported lazily by
``importlib.import_module`` at the moment one is needed, so this module never statically imports
``plugins`` (the table is checked against the built-in state manifests in the tests). A trained spec
(``training_window`` set) without a ``seed`` is refused before anything runs (ADR-0035).

Errors never print settings, DSNs or exception arguments of unexpected failures: only the failing
step and the exception type are written to stderr.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError

from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateProvider, StateResult, UnsupportedState
from core.domain.base import Contract, contract_schema_version_scope
from core.domain.specs import StateSpec
from infrastructure.catalog.definitions import TableDefinitionRegistry
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.settings import Settings
from infrastructure.state.iceberg import StateTable
from infrastructure.state.runner import StateRunnerError, run_state, state_inputs, state_request
from infrastructure.state.store import StateResultStore, StateStoreCorrupted
from infrastructure.state.table_definition import PHASE2_TABLES, STATE_STATES

__all__ = [
    "PROVIDER_PATHS",
    "CliError",
    "main",
    "open_store",
    "read_contract",
    "summary_lines",
]

#: Built-in ``StateProvider`` name -> ``module:Class`` (imported lazily; see module docs).
PROVIDER_PATHS: Final[dict[str, str]] = {
    "volatility_regime": "plugins.states.regimes:VolatilityRegimeProvider",
    "liquidity_regime": "plugins.states.regimes:LiquidityRegimeProvider",
    "trend_range": "plugins.states.regimes:TrendRangeProvider",
    "volatility_squeeze": "plugins.states.volatility_events:VolatilitySqueezeProvider",
    "return_shock": "plugins.states.volatility_events:ReturnShockProvider",
}


class CliError(Exception):
    """A failure whose message is safe to print (it never carries settings or credentials)."""


def read_contract[C: Contract](model: type[C], path: Path, what: str) -> C:
    """Rebuild a persisted contract object from a JSON file (at its recorded schema version).

    Validated in JSON mode: exact-decimal values are stored as text and must parse as ``Decimal``.
    """
    try:
        data = Path(path).read_bytes()
        document = json.loads(data)
    except (OSError, ValueError):
        raise CliError(f"cannot read {what} from {path}: not a readable JSON file") from None
    if not isinstance(document, dict):
        raise CliError(f"{what} {path} is not a JSON object")
    version = document.get("schema_version")
    try:
        if isinstance(version, str):
            with contract_schema_version_scope(version):
                return model.model_validate_json(data)
        return model.model_validate_json(data)
    except (ValidationError, ValueError, TypeError):
        raise CliError(f"{what} {path} is not a valid {model.__name__}") from None


def _provider_for(reference: str, spec: StateSpec) -> StateProvider:
    name, separator, version = reference.partition("@")
    path = PROVIDER_PATHS.get(name)
    if path is None or not separator or not version:
        known = ", ".join(sorted(PROVIDER_PATHS))
        raise CliError(f"unknown provider {reference!r}: expected name@version, name in [{known}]")
    module_name, _, class_name = path.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    try:
        provider = cast(StateProvider, cls([spec]))
    except (ValueError, TypeError) as exc:
        raise CliError(f"{spec.ref} cannot be served by {name}: {exc}") from None
    if provider.descriptor.plugin_key != reference:
        raise CliError(
            f"provider {reference} is not available (built-in: {provider.descriptor.plugin_key})"
        )
    return provider


def summary_lines(result: StateResult) -> list[str]:
    """A deterministic ``key<TAB>value`` summary of one run."""
    counts = Counter(item.state for item in result.values)
    lines = [
        f"result_hash\t{result.result_hash}",
        f"request_hash\t{result.request_hash}",
        f"provider\t{result.provider}",
        f"provider_hash\t{result.provider_hash}",
        f"evaluations\t{len(result.values)}",
        f"first_time\t{result.values[0].evaluation_time.isoformat()}",
        f"last_time\t{result.values[-1].evaluation_time.isoformat()}",
        f"not_computable\t{counts.get(None, 0)}",
    ]
    lines.extend(
        f"state\t{label}\t{count}"
        for label, count in sorted(
            ((label, count) for label, count in counts.items() if label is not None)
        )
    )
    return lines


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


def open_store(path: Path) -> StateResultStore:
    if not Path(path).is_dir():
        raise CliError(f"{path} is not an existing state store directory")
    return StateResultStore(path)


def _show(args: argparse.Namespace) -> int:
    store = open_store(args.store)
    try:
        result = store.get(args.result_hash)
    except KeyError:
        raise CliError(f"no state run {args.result_hash} in {args.store}") from None
    except ValueError:
        raise CliError("result_hash must be 64 lowercase hexadecimal characters") from None
    for line in summary_lines(result):
        print(line)
    print("verified\tyes")
    if args.values:
        for item in result.values:
            label = "" if item.state is None else item.state
            print(f"value\t{item.evaluation_time.isoformat()}\t{label}\t{item.inputs_used}")
    return 0


def _list(args: argparse.Namespace) -> int:
    for result_hash in open_store(args.store).hashes():
        print(result_hash)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run, read back and list State runs (ADR-0102). Inert unless asked."
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

    show = commands.add_parser("show", help="read one run back from a store and verify it")
    show.add_argument("--store", type=Path, required=True)
    show.add_argument("result_hash")
    show.add_argument("--values", action="store_true", help="also print every state value")
    show.set_defaults(handler=_show)

    listing = commands.add_parser("list", help="list the result hashes in a store")
    listing.add_argument("--store", type=Path, required=True)
    listing.set_defaults(handler=_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Any = args.handler
    try:
        return int(handler(args))
    except (CliError, StateRunnerError, UnsupportedState, StateStoreCorrupted) as exc:
        print(f"state {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Settings / catalog / storage errors can embed DSNs or paths: name the type only.
        print(f"state {args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
