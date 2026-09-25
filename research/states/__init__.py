"""Market-state research tooling (Phase 2; ADR-0035). Research only, never production (H5).

- ``diagnostics``: distribution, run durations, transition matrix and label flicker of one state
  series (``diagnose`` / ``render_markdown``).

StateProviders themselves live in ``plugins/states/`` behind ``core.contracts.state.StateProvider``;
the runner that truncates their inputs lives in ``infrastructure/state/``.
"""
