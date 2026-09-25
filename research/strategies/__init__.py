"""Phase 5 research strategy library (ADR-0038). Research code — never production (H5, ADR-0005).

- ``time_series_momentum``: ``TimeSeriesMomentumProvider`` (StrategyProvider) and its specs;
- ``volatility_target``: ``VolatilityTargetRiskProvider`` (RiskProvider) and ``vol_target_bars``;
- ``signals``: bar-derived signal observations for exploration;
- ``pipeline``: strategy → risk → backtest → validation hook → Failure Registry;
- ``validation``: the ``BacktestValidator`` seam and ``PipelineBacktestValidator`` (G0 – G4);
- ``failure_registry``: append-only ``FailureRecord`` store;
- ``library``: entries with sources and declared parameter spaces.
"""
