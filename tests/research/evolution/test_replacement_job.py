"""The replacement proposal job (Phase 12, ADR-0045 implementation note 2026-09-26): a research
job outside the loop, fed by the loop's durable lineage (read-only) and by what lives outside the
loop — incumbents from production, candidate lifecycles from the human Promotion path, verified
validation reports. Every lifecycle history and report here is TEST ONLY (``tests.promotion
.fixtures``: approvals by ``TEST-ONLY-human``); nothing is evidence about any strategy.
"""

from __future__ import annotations

import gc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.domain.research import ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.evolution import (
    PENDING_HUMAN_APPROVAL,
    LineageGraph,
    ProposalLedger,
    mutate,
)
from research.evolution.replacement_job import (
    LINEAGE_FILE,
    Incumbent,
    ReplacementCandidate,
    propose_replacements,
    read_lineage,
    reports_by_hash,
)
from research.loop import OosUnsealBudget, open_synthetic_loop
from research.loop import durable as loop_durable
from research.persistence import JournalCorrupted
from research.strategies.library import library_entries
from tests.promotion.fixtures import (
    PATH_TO_PAPER,
    PATH_TO_PRODUCTION_CANDIDATE,
    history,
    toy_experiment,
    toy_profile,
    toy_report,
)
from tests.research.loop import loop_fixtures as fx

S = LifecycleState
T1 = datetime(2026, 9, 26, tzinfo=UTC)
PATH_TO_ACTIVE = (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE)
PATH_TO_DEGRADED = (*PATH_TO_ACTIVE, S.DEGRADED)
PATH_TO_OOS = PATH_TO_PAPER[:-1]
ALL_STAGES = ("G0", "G1", "G2", "G3", "G4", "G5")
REASON = "{candidate} ({candidate_state}) re-validated; may replace {incumbent} ({incumbent_state})"
PROPOSER = "research_job:replacement"

INCUMBENT = library_entries()[0].candidate().spec  # tsmom_bars@1.0.0 (cites knowledge lineage)
STRANGER = library_entries()[1].candidate().spec  # tsmom_bars_vol_scaled@1.0.0
CHILD = mutate(INCUMBENT, "lookback", sorted(INCUMBENT.param_search_space["lookback"])[0]).spec


def _pass_report(spec: StrategySpec, report_id: str, stages: tuple[str, ...] = ALL_STAGES) -> Any:
    """TEST ONLY: a PASS report (G5 included unless ``stages`` omits it) about ``spec``."""
    return toy_report(spec, toy_experiment(spec), report_id, stages)


def _lineage(tmp_path: Path) -> LineageGraph:
    path = tmp_path / "loop_state" / LINEAGE_FILE
    LineageGraph((INCUMBENT, CHILD), path=path)
    return read_lineage(path)


def _run(ledger: ProposalLedger, lineage: LineageGraph, **overrides: Any) -> Any:
    report = _pass_report(CHILD, "rep-child")
    kwargs: dict[str, Any] = {
        "incumbents": (
            Incumbent(INCUMBENT, history(INCUMBENT.ref, PATH_TO_ACTIVE)),
            Incumbent(STRANGER, history(STRANGER.ref, PATH_TO_DEGRADED)),
        ),
        "candidates": (
            ReplacementCandidate(
                CHILD, history(CHILD.ref, PATH_TO_PAPER), (report.content_hash(),)
            ),
        ),
        "reports": reports_by_hash((report,)),
        "profiles": (toy_profile(),),
        "lineage": lineage,
        "ledger": ledger,
        "reason": REASON,
        "proposed_by": PROPOSER,
        "proposed_at": T1,
    }
    kwargs.update(overrides)
    return propose_replacements(**kwargs)


def test_a_paper_descendant_yields_one_pending_proposal_recorded_once(tmp_path: Path) -> None:
    lineage, path = _lineage(tmp_path), tmp_path / "proposals" / "proposals.jsonl"
    with ProposalLedger(path) as ledger:
        first = _run(ledger, lineage)
        [proposal] = first.recorded
        assert proposal.status == PENDING_HUMAN_APPROVAL
        assert (proposal.incumbent, proposal.candidate) == (str(INCUMBENT.ref), str(CHILD.ref))
        assert (proposal.incumbent_state, proposal.candidate_state) == (S.ACTIVE, S.PAPER)
        assert proposal.lineage_path == (str(CHILD.ref), str(INCUMBENT.ref))
        report = _pass_report(CHILD, "rep-child")
        assert proposal.evidence == (f"validation_report:{report.content_hash()}",)
        assert proposal.reason == REASON.format(
            candidate=CHILD.ref, candidate_state="PAPER", incumbent=INCUMBENT.ref,
            incumbent_state="ACTIVE",
        )  # fmt: skip
        # the non-descendant incumbent gets nothing; nothing was refused
        assert first.not_descendant == ((str(STRANGER.ref), str(CHILD.ref)),)
        assert first.refused == ()
        again = _run(ledger, lineage)
        assert again.recorded == ()
        assert again.already_proposed == (
            (str(INCUMBENT.ref), str(CHILD.ref), proposal.proposal_hash),
        )
    # a restart: a new ledger object on the same file never proposes the pair again
    with ProposalLedger(path) as reopened:
        assert reopened.proposals == (proposal,)
        assert _run(reopened, lineage).recorded == ()
        assert len(reopened.proposals) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert first.payload()["status"] == PENDING_HUMAN_APPROVAL


@pytest.mark.parametrize(
    ("path", "state"),
    [(PATH_TO_OOS, S.OOS), (PATH_TO_OOS[:3], S.VALIDATION), ((S.IDEA,), S.IDEA)],
)
def test_a_candidate_not_yet_paper_produces_no_proposal(
    tmp_path: Path, path: tuple[LifecycleState, ...], state: LifecycleState
) -> None:
    report = _pass_report(CHILD, "rep-child")
    with ProposalLedger(tmp_path / "p.jsonl") as ledger:
        result = _run(
            ledger,
            _lineage(tmp_path),
            candidates=(
                ReplacementCandidate(CHILD, history(CHILD.ref, path), (report.content_hash(),)),
            ),
        )
        assert result.recorded == () and ledger.proposals == ()
        [(_, _, reason)] = result.refused
        assert state.value in reason


def test_a_production_candidate_may_be_proposed_against_a_degraded_incumbent(
    tmp_path: Path,
) -> None:
    child = mutate(STRANGER, "lookback", 1440).spec
    lineage = LineageGraph((STRANGER, child))
    report = _pass_report(child, "rep-other")
    with ProposalLedger(tmp_path / "p.jsonl") as ledger:
        result = _run(
            ledger,
            lineage,
            candidates=(
                ReplacementCandidate(
                    child,
                    history(child.ref, PATH_TO_PRODUCTION_CANDIDATE),
                    (report.content_hash(),),
                ),
            ),
            reports=reports_by_hash((report,)),
        )
    [proposal] = result.recorded
    assert (proposal.incumbent_state, proposal.candidate_state) == (
        S.DEGRADED,
        S.PRODUCTION_CANDIDATE,
    )
    assert result.not_descendant == ((str(INCUMBENT.ref), str(child.ref)),)


def _refusal(
    tmp_path: Path,
    reports: tuple[ValidationReport, ...],
    hashes: tuple[str, ...],
    **overrides: Any,
) -> str:
    with ProposalLedger(tmp_path / "p.jsonl") as ledger:
        result = _run(
            ledger,
            _lineage(tmp_path),
            candidates=(ReplacementCandidate(CHILD, history(CHILD.ref, PATH_TO_PAPER), hashes),),
            reports=reports_by_hash(reports),
            **overrides,
        )
        assert result.recorded == () and ledger.proposals == ()
    [(incumbent, candidate, reason)] = result.refused
    assert (incumbent, candidate) == (str(INCUMBENT.ref), str(CHILD.ref))
    return str(reason)


def test_evidence_must_be_verified_reports(tmp_path: Path) -> None:
    """``research.router.evidence.check_report``: PASS, including G5, about the candidate."""
    no_g5 = _pass_report(CHILD, "rep-no-g5", ("G0", "G1", "G2", "G3", "G4"))
    other_subject = _pass_report(INCUMBENT, "rep-parent")
    failed = toy_report(
        CHILD, toy_experiment(CHILD), "rep-fail", ALL_STAGES, failing="G5", verdict=Verdict.FAIL
    )
    good = _pass_report(CHILD, "rep-good")
    assert "no validation report" in _refusal(tmp_path / "a", (), ())
    assert "report_not_found" in _refusal(tmp_path / "b", (), ("0" * 64,))
    assert "sealed_oos_not_evaluated" in _refusal(tmp_path / "c", (no_g5,), (no_g5.content_hash(),))
    assert "subject_mismatch" in _refusal(
        tmp_path / "d", (other_subject,), (other_subject.content_hash(),)
    )
    assert "verdict_not_pass" in _refusal(tmp_path / "e", (failed,), (failed.content_hash(),))
    # every claimed report must verify, not just one of them
    assert "sealed_oos_not_evaluated" in _refusal(
        tmp_path / "f", (good, no_g5), (good.content_hash(), no_g5.content_hash())
    )
    # the report's Profile must be given, and its ADR-0060 market benchmark item present
    assert "profile_not_found" in _refusal(
        tmp_path / "g", (good,), (good.content_hash(),), profiles=()
    )
    no_benchmark = toy_report(
        CHILD, toy_experiment(CHILD), "rep-no-mb", ALL_STAGES, market_benchmark=None
    )
    assert "market_benchmark_missing" in _refusal(
        tmp_path / "h", (no_benchmark,), (no_benchmark.content_hash(),)
    )
    # and, since the toy Profile reports the inverse control, its ADR-0060 item too
    assert toy_profile().benchmark.inverse_control_reported
    no_inverse = toy_report(
        CHILD, toy_experiment(CHILD), "rep-no-inverse", ALL_STAGES, inverse_control=None
    )
    assert "inverse_control_missing" in _refusal(
        tmp_path / "i", (no_inverse,), (no_inverse.content_hash(),)
    )


def test_incumbents_and_the_job_have_no_defaults_and_are_checked(tmp_path: Path) -> None:
    for path in (PATH_TO_PAPER, PATH_TO_PRODUCTION_CANDIDATE, PATH_TO_OOS):
        with pytest.raises(ValueError, match="running in production"):
            Incumbent(INCUMBENT, history(INCUMBENT.ref, path))
    with pytest.raises(ValueError, match="belongs to"):
        Incumbent(INCUMBENT, history(STRANGER.ref, PATH_TO_ACTIVE))
    with ProposalLedger(tmp_path / "p.jsonl") as ledger:
        lineage = _lineage(tmp_path)
        for overrides, match in (
            ({"reason": " "}, "reason"),
            ({"reason": "because {sharpe}"}, "reason template"),
            ({"proposed_by": ""}, "proposer"),
        ):
            with pytest.raises(ValueError, match=match):
                _run(ledger, lineage, **overrides)
        with pytest.raises(TypeError):
            propose_replacements(incumbents=(), candidates=())  # type: ignore[call-arg]
        assert ledger.proposals == ()


def test_the_job_never_changes_a_lifecycle(tmp_path: Path) -> None:
    incumbent_history = history(INCUMBENT.ref, PATH_TO_ACTIVE)
    candidate_history = history(CHILD.ref, PATH_TO_PAPER)
    report = _pass_report(CHILD, "rep-child")
    with ProposalLedger(tmp_path / "p.jsonl") as ledger:
        result = _run(
            ledger,
            _lineage(tmp_path),
            incumbents=(Incumbent(INCUMBENT, incumbent_history),),
            candidates=(ReplacementCandidate(CHILD, candidate_history, (report.content_hash(),)),),
        )
    assert len(result.recorded) == 1
    assert incumbent_history == history(INCUMBENT.ref, PATH_TO_ACTIVE)  # immutable, unchanged
    assert candidate_history.current_state is S.PAPER
    assert "approved_by" not in result.recorded[0].to_payload()


def test_read_lineage_verifies_and_never_writes(tmp_path: Path) -> None:
    assert LINEAGE_FILE == loop_durable.LINEAGE_FILE
    with pytest.raises(FileNotFoundError):
        read_lineage(tmp_path / "missing" / LINEAGE_FILE)
    path = tmp_path / LINEAGE_FILE
    LineageGraph((INCUMBENT, CHILD), path=path)
    before = path.read_bytes()
    graph = read_lineage(path)
    assert [s.ref for s in graph.specs] == [INCUMBENT.ref, CHILD.ref]
    assert graph.journal is None and path.read_bytes() == before
    path.write_text(before.decode().replace("lookback", "lookbaxk", 1), encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        read_lineage(path)


# ------------------------------------------------ against a real loop state directory (read-only)


@pytest.fixture(scope="module")
def loop_state(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[ValidationReport]]:
    """Two rounds of the durable synthetic loop (TEST ONLY, ``loop_fixtures``): round 1 evolves
    ``tsmom_bars@1.1.0`` from the library's ``tsmom_bars@1.0.0``. Returns the state directory
    and the loop's own reports about the offspring."""
    state_dir = tmp_path_factory.mktemp("loop") / "state"
    config = fx.config(
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(
            evolution=True,
            oos_unseal=OosUnsealBudget(
                max_unsealings=1, approved_families={fx.FAMILY: "test-human"}
            ),
        ),
    )
    llm = ScriptedLLMProvider([fx.llm_output(i, None) for i in range(2)], clock=lambda: fx.T0)
    with open_synthetic_loop(
        config,
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=llm,
    ) as durable:
        durable.loop.run_unattended(2)
        [row] = durable.memory.offspring
        assert row["parent"] == str(INCUMBENT.ref)
        reports = [
            v.report
            for v in durable.memory.validations
            if v.report is not None
            and v.outcome.candidate is not None
            and str(v.outcome.candidate.spec.ref) == row["child"]
        ]
    gc.collect()
    return state_dir, reports


def test_the_job_reads_a_loop_state_directory_and_leaves_it_untouched(
    loop_state: tuple[Path, list[ValidationReport]], tmp_path: Path
) -> None:
    state_dir, loop_reports = loop_state
    before = {p.name: p.read_bytes() for p in state_dir.iterdir() if p.is_file()}
    lineage = read_lineage(state_dir / LINEAGE_FILE)
    child = next(s for s in lineage.specs if s.ref != INCUMBENT.ref)
    assert INCUMBENT.ref in child.lineage and child.version == "1.1.0"
    ledger_path, anchor = tmp_path / "ledger" / "proposals.jsonl", tmp_path / "anchor.jsonl"
    incumbents = (Incumbent(INCUMBENT, history(INCUMBENT.ref, PATH_TO_ACTIVE)),)
    paper = history(child.ref, PATH_TO_PAPER)  # the TEST ONLY human Promotion path's record
    with ProposalLedger(ledger_path, anchor=anchor) as ledger:
        # the loop's own reports about the offspring cannot back a replacement: the loop never
        # ran G5 on it (the family's one unsealing was spent in round 0)
        assert loop_reports
        loop_only = propose_replacements(
            incumbents=incumbents,
            candidates=(
                ReplacementCandidate(child, paper, tuple(r.content_hash() for r in loop_reports)),
            ),
            reports=reports_by_hash(loop_reports),
            profiles=(fx.loop_profile(boundary_day=3), toy_profile()),
            lineage=lineage,
            ledger=ledger,
            reason=REASON,
            proposed_by=PROPOSER,
            proposed_at=T1,
        )
        assert loop_only.recorded == ()
        [(_, _, reason)] = loop_only.refused
        assert "not_pass" in reason or "sealed_oos_not_evaluated" in reason
        report = _pass_report(child, "rep-loop-child")  # TEST ONLY PASS incl. G5
        result = propose_replacements(
            incumbents=incumbents,
            candidates=(ReplacementCandidate(child, paper, (report.content_hash(),)),),
            reports=reports_by_hash((report,)),
            profiles=(toy_profile(),),
            lineage=lineage,
            ledger=ledger,
            reason=REASON,
            proposed_by=PROPOSER,
            proposed_at=T1,
        )
        [proposal] = result.recorded
        assert proposal.lineage_path == (str(child.ref), str(INCUMBENT.ref))
    # read-only: every file of the loop's state directory is byte-identical, none was added
    assert {p.name: p.read_bytes() for p in state_dir.iterdir() if p.is_file()} == before
