"""Phase 5 research strategy library (ADR-0038). Research code — never production (H5, ADR-0005).

- ``time_series_momentum``: ``TimeSeriesMomentumProvider`` (StrategyProvider) and its specs;
- ``cross_sectional_momentum``: ``CrossSectionalMomentumProvider`` and ``xsmom_bars``;
- ``volatility_target``: ``VolatilityTargetRiskProvider`` (RiskProvider) and ``vol_target_bars``;
- ``donchian_breakout``: ``DonchianBreakoutProvider`` and ``donchian_breakout`` (ADR-0085);
- ``zscore_reversion``: ``ZScoreReversionProvider`` and ``zscore_reversion`` (ADR-0085);
- ``dual_momentum``: ``DualMomentumProvider`` and ``dual_momentum`` (ADR-0085);
- ``composite``: ``CompositeStrategyProvider`` for ``StrategySpec.composition`` (conditioned /
  ensemble / negated; ADR-0088 decision 2), with caller-injected ``ResolvedStrategy`` parts;
- ``drawdown_control``: ``DrawdownControlRiskProvider`` and ``drawdown_control`` (ADR-0085;
  ``PortfolioState.peak_equity`` from ADR-0088 decision 3);
- ``signals``: bar-derived signal observations for exploration;
- ``price_signals``: ``bar_close`` / ``bar_high`` / ``bar_low`` observations for exploration;
- ``pipeline``: strategy → risk → backtest → validation hook → Failure Registry;
- ``validation``: the ``BacktestValidator`` seam and ``PipelineBacktestValidator`` (G0 – G4);
- ``failure_registry``: append-only ``FailureRecord`` store;
- ``library``: entries with sources and declared parameter spaces.
"""
