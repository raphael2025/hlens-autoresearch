"""Locks ``research/strategies/price_signals.py``'s ``BAR_CLOSE/HIGH/LOW_SIGNAL`` refs to the
identities ``plugins/features/indicators.py``'s ``BarCloseProvider`` / ``BarHighProvider`` /
``BarLowProvider`` actually publish (``bar_close`` / ``bar_high`` / ``bar_low`` @ ``1.0.0``).

``indicators.py`` notes it could not cross-check these strings against this module because this
module did not exist yet in that worktree; now that it does, this test is that cross-check."""

from __future__ import annotations

from plugins.features.indicators import BarCloseProvider, BarHighProvider, BarLowProvider
from research.strategies.price_signals import BAR_CLOSE_SIGNAL, BAR_HIGH_SIGNAL, BAR_LOW_SIGNAL


def test_bar_price_signal_refs_match_the_feature_provider_identities() -> None:
    assert BAR_CLOSE_SIGNAL == BarCloseProvider.spec().ref
    assert BAR_HIGH_SIGNAL == BarHighProvider.spec().ref
    assert BAR_LOW_SIGNAL == BarLowProvider.spec().ref
