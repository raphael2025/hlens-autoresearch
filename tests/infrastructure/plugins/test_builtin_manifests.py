"""Built-in `PluginManifest`s vs. the real Provider classes they describe (ADR-0087 task 2).

ADR-0087's own "后果" names the risk directly: a manifest can drift from the provider it claims to
describe. This suite instantiates every built-in Provider the same way the rest of the test suite
already does (minimal, valid specs — see `tests/plugins/events/`,
`tests/test_state_contract_suite.py`, `tests/research/synthetic_lab/gate_fixtures.py` for the
patterns this reuses) and compares its own
`descriptor.name` / `descriptor.version` against the corresponding `infrastructure.plugins.builtin`
manifest's `name` / `version`, one pair at a time — plus the manifest's `kind` against the Provider
family it belongs to. A provider whose class-level `NAME` / `VERSION` changes without its manifest
being updated fails exactly the pair that drifted, not a generic diff.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.domain.base import Kind, Ref
from core.domain.specs import OutcomeSpec
from infrastructure.plugins.builtin import BUILTIN_MANIFESTS
from infrastructure.plugins.builtin.backtest import HLENS_BAR_BACKTEST
from infrastructure.plugins.builtin.events import (
    EVENT_ABSENCE,
    EVENT_CO_OCCURRENCE,
    EVENT_COUNT,
    EVENT_SEQUENCE,
    EVENT_WINDOW_END,
    FEATURE_THRESHOLD_CROSS,
    STATE_SWITCH,
    VOLATILITY_BREAKOUT,
)
from infrastructure.plugins.builtin.features import (
    BAR_LOG_RETURN,
    BAR_REALIZED_VOLATILITY,
    BAR_VOLUME_SUM,
)
from infrastructure.plugins.builtin.knowledge import HLENS_KNOWLEDGE_LOCAL
from infrastructure.plugins.builtin.llm import HLENS_LLM_SCRIPTED
from infrastructure.plugins.builtin.outcomes import HLENS_FORWARD_RETURN, HLENS_TRIPLE_BARRIER
from infrastructure.plugins.builtin.states import LIQUIDITY_REGIME, TREND_RANGE, VOLATILITY_REGIME
from infrastructure.plugins.builtin.synthetic import HLENS_SYNTHETIC_RANDOM_WALK
from infrastructure.plugins.manifest import PluginKind, PluginManifest
from plugins.backtest import BarBacktester
from plugins.events import (
    EventAbsenceProvider,
    EventCoOccurrenceProvider,
    EventCountProvider,
    EventSequenceProvider,
    EventWindowEndProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
    VolatilityBreakoutProvider,
)
from plugins.features import (
    BarLogReturnProvider,
    BarRealizedVolatilityProvider,
    BarVolumeSumProvider,
)
from plugins.knowledge import LocalKnowledgeProvider
from plugins.llm import ScriptedLLMProvider
from plugins.outcomes import ForwardReturnOutcome, TripleBarrierOutcome
from plugins.states import LiquidityRegimeProvider, TrendRangeProvider, VolatilityRegimeProvider
from plugins.synthetic import RandomWalkMarket

MINUTE = timedelta(minutes=1)
X = Ref(kind=Kind.FEATURE, name="x", version="1.0.0")
VOLUME_X = Ref(kind=Kind.FEATURE, name="volume_x", version="1.0.0")


def _quantile_spec(provider_cls: type, feature: Ref) -> object:
    return provider_cls.spec(
        feature, cuts=["0.33", "0.66"], min_history=5, training_window=timedelta(hours=1), seed=1
    )


def _outcome_label_spec(method: OutcomeMethod, **barriers: Decimal) -> OutcomeLabelSpec:
    outcome_spec = OutcomeSpec(
        name="test_outcome", version="1.0.0", horizon=timedelta(hours=1), label_definition="test"
    )
    return OutcomeLabelSpec.bind(outcome_spec, method, **barriers)


# ---------------------------------------------------------------- one descriptor per provider


def _build_cases() -> list[tuple[object, PluginManifest, str]]:
    """`(provider, its manifest, expected PluginKind value)` for every built-in Provider."""
    log_return = BarLogReturnProvider.spec()
    trend = TrendRangeProvider.spec(log_return.ref, window=5, threshold="0.5")
    cross_up = FeatureThresholdCrossProvider.spec(log_return.ref, Decimal("0"), "up")
    switch = StateSwitchProvider.spec(trend.ref)
    return [
        (BarLogReturnProvider((log_return,)), BAR_LOG_RETURN, "feature"),
        (
            BarRealizedVolatilityProvider((BarRealizedVolatilityProvider.spec(3),)),
            BAR_REALIZED_VOLATILITY,
            "feature",
        ),
        (BarVolumeSumProvider((BarVolumeSumProvider.spec(3),)), BAR_VOLUME_SUM, "feature"),
        (
            VolatilityRegimeProvider((_quantile_spec(VolatilityRegimeProvider, X),)),
            VOLATILITY_REGIME,
            "state",
        ),
        (
            LiquidityRegimeProvider((_quantile_spec(LiquidityRegimeProvider, VOLUME_X),)),
            LIQUIDITY_REGIME,
            "state",
        ),
        (TrendRangeProvider((trend,)), TREND_RANGE, "state"),
        (FeatureThresholdCrossProvider((cross_up,)), FEATURE_THRESHOLD_CROSS, "event"),
        (
            VolatilityBreakoutProvider(
                (VolatilityBreakoutProvider.spec(log_return.ref, 3, Decimal("2")),)
            ),
            VOLATILITY_BREAKOUT,
            "event",
        ),
        (StateSwitchProvider((switch,)), STATE_SWITCH, "event"),
        (
            EventSequenceProvider((EventSequenceProvider.spec(cross_up, switch, 3 * MINUTE),)),
            EVENT_SEQUENCE,
            "event",
        ),
        (
            EventCoOccurrenceProvider((EventCoOccurrenceProvider.spec(cross_up, switch, MINUTE),)),
            EVENT_CO_OCCURRENCE,
            "event",
        ),
        (
            EventWindowEndProvider(
                (EventWindowEndProvider.spec(cross_up, 2 * MINUTE, name="up_window_end"),)
            ),
            EVENT_WINDOW_END,
            "event",
        ),
        (
            EventAbsenceProvider(
                (EventAbsenceProvider.spec(cross_up, switch, MINUTE, name="up_without_switch"),)
            ),
            EVENT_ABSENCE,
            "event",
        ),
        (
            EventCountProvider(
                (EventCountProvider.spec(switch, 2, 3 * MINUTE, name="switch_twice"),)
            ),
            EVENT_COUNT,
            "event",
        ),
        (
            ForwardReturnOutcome((_outcome_label_spec(OutcomeMethod.FORWARD_RETURN),)),
            HLENS_FORWARD_RETURN,
            "outcome",
        ),
        (
            TripleBarrierOutcome(
                (
                    _outcome_label_spec(
                        OutcomeMethod.TRIPLE_BARRIER,
                        upper_barrier=Decimal("0.02"),
                        lower_barrier=Decimal("0.01"),
                    ),
                )
            ),
            HLENS_TRIPLE_BARRIER,
            "outcome",
        ),
        (BarBacktester(), HLENS_BAR_BACKTEST, "backtest"),
        (RandomWalkMarket(), HLENS_SYNTHETIC_RANDOM_WALK, "synthetic"),
        (ScriptedLLMProvider(outputs=[{}]), HLENS_LLM_SCRIPTED, "llm"),
        (LocalKnowledgeProvider(), HLENS_KNOWLEDGE_LOCAL, "knowledge"),
    ]


_CASES = _build_cases()


@pytest.mark.parametrize(
    "provider, manifest, expected_kind",
    _CASES,
    ids=[manifest.name for _, manifest, _ in _CASES],
)
def test_builtin_manifest_matches_its_provider_name_version_kind(
    provider: object, manifest: PluginManifest, expected_kind: str
) -> None:
    descriptor = provider.descriptor  # type: ignore[attr-defined]
    assert manifest.name == descriptor.name
    assert manifest.version == descriptor.version
    assert manifest.kind.value == expected_kind


def test_every_case_is_covered_by_builtin_manifests_with_no_leftovers() -> None:
    covered = {manifest.plugin_key for _, manifest, _ in _CASES}
    declared = {manifest.plugin_key for manifest in BUILTIN_MANIFESTS}
    assert covered == declared


def test_builtin_manifests_have_no_duplicate_name_at_version_within_a_kind() -> None:
    keys = [(manifest.kind, manifest.plugin_key) for manifest in BUILTIN_MANIFESTS]
    assert len(keys) == len(set(keys))


def test_builtin_manifests_cover_every_kind_except_strategy_and_risk() -> None:
    # strategy / risk providers live in research/strategies/ and research/risk/ (not promoted,
    # ADR-0005) and infrastructure/ must not import research/ — see
    # test_plugins_and_infrastructure_do_not_import_research in test_architecture_boundaries.py.
    covered = {manifest.kind for manifest in BUILTIN_MANIFESTS}
    assert covered == set(PluginKind) - {PluginKind.STRATEGY, PluginKind.RISK}
