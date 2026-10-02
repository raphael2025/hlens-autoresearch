"""Market-state research tooling (Phase 2; ADR-0035). Research only, never production (H5).

- ``diagnostics``: distribution, run durations, transition matrix and label flicker of one state
  series (``diagnose`` / ``render_markdown``);
- ``run_cli`` / ``report_cli`` (ADR-0102): ``compute`` a state over persisted feature results, and
  report diagnostics of a stored run.

StateProviders themselves live in ``plugins/states/`` behind ``core.contracts.state.StateProvider``;
the runner that truncates their inputs lives in ``infrastructure/state/``.
"""
