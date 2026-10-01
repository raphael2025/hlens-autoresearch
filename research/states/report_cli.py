"""State stability report from a stored run (ADR-0102; Phase 2; research only, never production).

``python -m research.states.report_cli --store STORE --spec SPEC.json --min-run N RESULT_HASH``
reads one run from a ``StateResultStore`` (content and canonical form verified), takes the state
space from the explicit ``StateSpec`` JSON, and computes ``diagnose``. ``--min-run`` is the caller's
flicker parameter (no default, not a validation threshold). Without ``--out`` the Markdown report is
only printed; ``--out DIR`` additionally writes the deterministic JSON report to
``DIR/state_diagnostics/<diagnostics_hash>.json`` (``write_state_diagnostics``, append-only).

Errors never print exception arguments of unexpected failures: only the step and exception type.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from core.domain.specs import StateSpec
from infrastructure.state.run_cli import CliError, open_store, read_contract
from infrastructure.state.store import StateStoreCorrupted
from research.reports.envelope import ReportConflict
from research.reports.state_diagnostics import write_state_diagnostics
from research.states.diagnostics import diagnose, render_markdown

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnostics report of one stored State run (ADR-0102). Writes only with --out."
    )
    parser.add_argument("--store", type=Path, required=True, help="StateResultStore directory")
    parser.add_argument("--spec", type=Path, required=True, help="StateSpec JSON (state space)")
    parser.add_argument(
        "--min-run", type=int, required=True, help="flicker parameter: runs shorter than this"
    )
    parser.add_argument("--out", type=Path, help="report root; writes <out>/state_diagnostics/<id>")
    parser.add_argument("result_hash")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = read_contract(StateSpec, args.spec, "state spec")
        try:
            result = open_store(args.store).get(args.result_hash)
        except KeyError:
            raise CliError(f"no state run {args.result_hash} in {args.store}") from None
        report = diagnose(result, spec.state_space, min_run=args.min_run)
        print(render_markdown(report))
        if args.out is not None:
            written = write_state_diagnostics(args.out, report)
            status = "written" if written.written else "already present (identical)"
            print(f"report\t{written.path}\t{status}")
    except (CliError, StateStoreCorrupted, ReportConflict, ValueError) as exc:
        print(f"state report failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"state report failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
