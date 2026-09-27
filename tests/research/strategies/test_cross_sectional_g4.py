"""ADR-0059 (Accepted 2026-09-26): G4 cross-asset (C-R3) for cross-sectional strategies, end to end.

Phase 8 implementation note (2026-09-26; CODE_COMPLETE / DEBUG_PENDING). !!! TEST ONLY !!! The
Profile / cost / robustness values are the lax, uncalibrated TEST ONLY fixtures of
``test_multi_instrument_validation`` (``PARAMS``: cross-asset fraction 0.5); no verdict here is
research evidence.

- single-instrument and time-series strategies: report, view and G4 hashes pinned **before** the
  change (computed on 4543036) are reproduced byte for byte, with the same ``TrialRunner`` calls;
- ``xsmom_bars`` (declared cross-sectional) no longer structurally FAILs C-R3: it is judged over
  sub-universes (A), or INCONCLUSIVE when fewer than two exist; a noise book can still FAIL;
- the same rule, not declared, gets the zero-exposure INCONCLUSIVE (C) — the declaration is never
  inferred from results;
- sub-universe re-runs are robustness re-runs of the chosen trial: the trial count is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.domain.research import Verdict
from core.domain.specs import StrategySpec
from plugins.backtest import BarBacktester
from research.strategies.cross_section import CROSS_SECTIONAL_STRATEGIES, is_cross_sectional
from research.strategies.cross_sectional_momentum import (
    XSMOM_NAME,
    CrossSectionalMomentumProvider,
    xsmom_spec,
)
from research.strategies.pipeline import CandidateTrialRunner, StrategyCandidate
from research.strategies.time_series_momentum import tsmom_spec, tsmom_vol_scaled_spec
from research.strategies.validation import PipelineBacktestValidator, TrialRun, TrialRunner
from research.validation.g4 import RobustnessInput
from research.validation.report import to_json
from research.validation.returns import from_backtest
from research.validation.robustness import (
    NOT_ENOUGH_FOR_SUBUNIVERSES,
    SUBUNIVERSE_RULE,
    ZERO_EXPOSURE_SINGLE_ASSET,
    RobustnessCheck,
)
from tests.research.strategies import test_backtest_validation as single
from tests.research.strategies import test_cross_sectional_momentum as xs
from tests.research.strategies import test_multi_instrument_validation as multi
from tests.research.synthetic_lab import gate_fixtures as lax

CHOSEN = xs.CHOSEN
FRACTION = "G4.cross_asset.positive_fraction"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# =========================================================================================
# byte-identity of single-instrument and time-series strategies
# =========================================================================================

#: Computed on 4543036 (before ADR-0059 was implemented) with ``created_at = T0``. Re-pinned for
#: ADR-0060 enforcement (2026-09-26): the TEST ONLY Profiles' ``benchmark`` block changed from the
#: placeholder ``"test-only"`` to ``buy_and_hold_equal_weight`` + inverse control, so every
#: report / view hash (they bind the Profile) changed; nothing here opts in, and the G4
#: diagnostic hashes are unchanged.
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 6dd0103a…, 150814f2…, 53bb8fe3…, 2e445144…,
#: 92190e46…, b84dede4…, 95a83d79…, 9a0142ee…
PINNED = {
    "single_planted_report": "d5ca922f79b7c69b4bb6dd7364803b1e4a9cb76f9580cf07a3ca96cca4c94e88",
    "single_planted_view": "ab660a9f796234ed07048cac342d50f8f296361598ecdd1dbe0a772ff4e25b08",
    "multi_p,p_report": "c90fb6994c3023f5d7399b785ce3af5c3da52a8c85ce0e781ee23d07ee621735",
    "multi_p,p_view": "679840ded32bbca99a871439caa834efc8a885f58386277cf6bedca07841ef93",
    "multi_p,p_g4": "e95b1e91393213608ca9e06527fc3a48adca51ce273f43755f81a1b289920629",
    "multi_p,n,p_report": "4d2a52e5b8826843fbd3030573c3543cea7cc36ac57127f30ec6811367b75b8d",
    "multi_p,n,p_view": "c941d036f46b081479503dba9b4171f168cf4833c3ba828d387233a807c7965f",
    "multi_p,n,p_g4": "87af742d10d892831e0b45e96ea5f0cd385228db24a5a0ad8808aaa3aeda37a3",
}


def test_single_instrument_and_time_series_reports_are_byte_identical(tmp_path: Path) -> None:
    candidate = multi._candidate()  # tsmom_bars: time series, not declared cross-sectional
    assert not is_cross_sectional(candidate.spec)
    found: dict[str, str] = {}
    market = single._market(seed=7, planted=True)
    ctx = replace(single._context(candidate), created_at=single.T0)
    planted, _ = single._evaluate(market, tmp_path / "a", context=ctx)
    assert planted.validation is not None and planted.validation.view is not None
    found["single_planted_report"] = planted.validation.report.content_hash()
    found["single_planted_view"] = _sha(to_json(planted.validation.view))
    for kinds in ("p,p", "p,n,p"):
        book = multi.Book.of(kinds)
        context = replace(lax.context(candidate, lax.LAX_TEST_ONLY_PROFILE), created_at=lax.T0)
        setup = multi._setup(book, context=context)
        backtest = CandidateTrialRunner(candidate, book.inputs(), BarBacktester()).run(CHOSEN)
        validator = PipelineBacktestValidator(setup)
        answer = validator.validate(candidate.spec.ref, candidate.spec, backtest.backtest)
        assert answer.view is not None
        found[f"multi_{kinds}_report"] = answer.report.content_hash()
        found[f"multi_{kinds}_view"] = _sha(to_json(answer.view))
        diagnostic = validator.robustness_diagnostic(candidate.spec, backtest.backtest)
        found[f"multi_{kinds}_g4"] = _sha(json.dumps(diagnostic.to_dict(), sort_keys=True))
        g4 = validator.robustness_input(candidate.spec, backtest.backtest)
        assert g4.sub_universes is None  # a time-series strategy gets no sub-universe re-run
        assert g4.per_asset_exposed == dict.fromkeys(book.names, True)
    assert found == PINNED


# =========================================================================================
# declaration: by spec name only, never inferred from results
# =========================================================================================


def _renamed(name: str) -> StrategySpec:
    return StrategySpec.model_validate({**xsmom_spec().model_dump(), "name": name})


def _candidate(spec: StrategySpec) -> StrategyCandidate:
    return StrategyCandidate(
        spec=spec,
        strategy=CrossSectionalMomentumProvider((spec,)),
        hypothesis_family_id="xsmom_bars",
    )


def test_the_declaration_is_a_static_set_of_spec_names() -> None:
    assert frozenset({XSMOM_NAME}) == CROSS_SECTIONAL_STRATEGIES
    assert is_cross_sectional(xsmom_spec())
    assert not is_cross_sectional(tsmom_spec())
    assert not is_cross_sectional(tsmom_vol_scaled_spec())
    # The same rule under another name is not declared: behaviour does not make it one.
    assert not is_cross_sectional(_renamed("xsmom_bars_undeclared"))


# =========================================================================================
# the G4 input and the cross-asset verdicts of a cross-sectional strategy
# =========================================================================================


class _Counting:
    def __init__(self, inner: TrialRunner) -> None:
        self.inner = inner
        self.calls: list[tuple[str, ...] | None] = []

    def run(self, params, **kwargs) -> TrialRun:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs.get("instruments"))
        return self.inner.run(params, **kwargs)


def _g4(
    kinds: str, candidate: StrategyCandidate | None = None
) -> tuple[RobustnessInput, RobustnessCheck, _Counting, multi.Book]:
    book = multi.Book.of(kinds)
    candidate = candidate or xs._entry().candidate()
    runner = CandidateTrialRunner(candidate, book.inputs(), BarBacktester())
    counting = _Counting(runner)
    setup = replace(xs._setup(book, candidate), trials=counting)
    backtest = runner.run(CHOSEN).backtest
    validator = PipelineBacktestValidator(setup)
    g4 = validator.robustness_input(candidate.spec, backtest)
    result = validator.robustness_diagnostic(candidate.spec, backtest).result
    check = next(item for item in result.checks if item.check_id == "cross_asset")
    return g4, check, counting, book


def _fraction(check: RobustnessCheck) -> tuple[Verdict, str, float]:
    gate = next(g for g in check.gates if g.gate_id == FRACTION)
    return gate.verdict, gate.metric, gate.value


@pytest.fixture(scope="module")
def four_planted() -> tuple[RobustnessInput, RobustnessCheck, _Counting, multi.Book]:
    return _g4("p,p,p,p")


def test_sub_universes_are_re_run_and_judged(
    four_planted: tuple[RobustnessInput, RobustnessCheck, _Counting, multi.Book],
) -> None:
    g4, check, _, book = four_planted
    assert g4.per_asset_exposed == dict.fromkeys(book.names, False)  # flat alone, by definition
    assert g4.sub_universes is not None
    assert [s.instruments for s in g4.sub_universes] == [
        ("S0-USDT", "S1-USDT"),
        ("S2-USDT", "S3-USDT"),
    ]
    runner = CandidateTrialRunner(xs._entry().candidate(), book.inputs(), BarBacktester())
    for item in g4.sub_universes:
        run = runner.run(CHOSEN, instruments=item.instruments)
        assert item.returns == from_backtest(run.backtest) and item.exposed and run.backtest.fills
    # Not the structural FAIL of ADR-0059's context: a real sub-universe verdict (A).
    verdict, metric, value = _fraction(check)
    assert (verdict, metric, value) == (Verdict.PASS, "positive_subuniverse_fraction[>=]", 1.0)
    section = check.details["cross_section"]
    assert section["rule"] == SUBUNIVERSE_RULE  # type: ignore[index]
    assert "zero_exposure" not in check.details


def test_sub_universe_runs_add_no_trial(
    four_planted: tuple[RobustnessInput, RobustnessCheck, _Counting, multi.Book],
) -> None:
    g4, _, counting, book = four_planted
    grid = 1
    for values in xsmom_spec().param_search_space.values():
        grid *= len(values)
    assert g4.family_trial_count == grid == 12
    assert len(g4.trials) == grid
    # robustness_input and robustness_diagnostic each: re-run + other grid points + delay +
    # one offset + one run per declared asset + one run per sub-universe.
    per_call = 1 + (grid - 1) + 1 + 1 + len(book.names) + 2
    assert len(counting.calls) == 2 * per_call
    assert [c for c in counting.calls[:per_call] if c is not None] == [
        *((name,) for name in book.names),
        ("S0-USDT", "S1-USDT"),
        ("S2-USDT", "S3-USDT"),
    ]


def test_a_noise_cross_sectional_book_can_still_fail() -> None:
    _, check, _, _ = _g4("n,n,n,n")
    assert _fraction(check) == (Verdict.FAIL, "positive_subuniverse_fraction[>=]", 0.0)


def test_too_few_instruments_for_sub_universes_is_inconclusive() -> None:
    g4, check, _, _ = _g4("p,n")
    assert g4.sub_universes == ()
    verdict, metric, _ = _fraction(check)
    assert (verdict, metric) == (Verdict.INCONCLUSIVE, NOT_ENOUGH_FOR_SUBUNIVERSES)


def test_the_undeclared_same_rule_gets_the_zero_exposure_inconclusive() -> None:
    """C, and proof the declaration is not inferred: flat single-asset runs do not make the
    renamed strategy cross-sectional; it is not re-run on sub-universes either."""
    g4, check, counting, book = _g4("p,p,p,p", _candidate(_renamed("xsmom_bars_undeclared")))
    assert g4.sub_universes is None
    assert all(len(c) == 1 for c in counting.calls if c is not None)
    assert g4.per_asset_exposed == dict.fromkeys(book.names, False)
    verdict, metric, value = _fraction(check)
    assert (verdict, metric, value) == (Verdict.INCONCLUSIVE, ZERO_EXPOSURE_SINGLE_ASSET, 4.0)
