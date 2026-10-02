"""ADR-0105 §3 (second item): ``research.loop.state_export`` reads a durable loop state read-only
and exports one recorded run's artifacts (run, validation report, strategy spec, cost model,
label spec) as files.

The state is a real two-round durable synthetic loop (``loop_fixtures``; every number TEST ONLY,
evolution on). The export must never touch the state directory, must take only what the state
recorded (each object bound by the content hash the state and the run recorded) and must never
overwrite a file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.domain.research import ExperimentRun, ValidationReport
from core.domain.specs import OutcomeSpec, StrategySpec
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import open_synthetic_loop
from research.loop.durable import AUDIT_FILE, MEMORY_FILE
from research.loop.state_export import (
    COST_MODEL_FILE,
    EXPERIMENT_RUN_FILE,
    LABEL_SPEC_FILE,
    STRATEGY_SPEC_FILE,
    VALIDATION_REPORT_FILE,
    DurableRun,
    DurableStateView,
    StateExportRefused,
    export_run_artifacts,
    read_durable_state,
)
from research.strategies.library import library_entries
from tests.research.loop import loop_fixtures as fx

ROUNDS = 2


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``root``: content digest and modification time (ns)."""
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("state-export") / "state"
    config = fx.config(
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(evolution=True),
    )
    llm = ScriptedLLMProvider(
        [fx.llm_output(i, lookback) for i, lookback in enumerate((None, 240))],
        clock=lambda: fx.T0,
    )
    durable = open_synthetic_loop(config, state_dir=root, provider=RandomWalkMarket(), llm=llm)
    try:
        durable.loop.run_unattended(ROUNDS)
        assert len(durable.loop.audit.records) == ROUNDS
    finally:
        durable.close()
    return root


@pytest.fixture(scope="module")
def view(state_dir: Path) -> DurableStateView:
    return read_durable_state(state_dir)


def _configured(view: DurableStateView) -> DurableRun:
    """A run of the configured strategy (not held by the state) that has one report."""
    ref = str(_tsmom().ref)
    return next(item for item in view.runs if item.strategy == ref and len(item.reports) == 1)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    target = tmp_path / "export"
    target.mkdir()
    return target


def _tsmom() -> StrategySpec:
    spec: StrategySpec = library_entries()[0].candidate().spec
    return spec


# ---- reading ------------------------------------------------------------------------------


def test_the_view_holds_every_recorded_run_with_its_report(view: DurableStateView) -> None:
    assert view.rounds == ROUNDS
    assert view.fingerprint["cost_model"] == fx.COST_MODEL.content_hash()
    assert view.fingerprint["label_spec"] == fx.LABEL_SPEC.content_hash()
    assert view.runs, "the loop recorded runs"
    assert {item.round_index for item in view.runs} <= set(range(ROUNDS))
    for item in view.runs:
        for report in item.reports:
            assert report.run_id == item.run.run_id
            assert report.experiment_hash == item.run.experiment_hash
    # the configured strategy is configuration: the state holds only its hash
    assert str(_tsmom().ref) not in view.strategies
    assert all(spec.lineage for spec in view.strategies.values())  # evolved specs only


def test_reading_and_exporting_leave_the_state_directory_untouched(
    state_dir: Path, out: Path
) -> None:
    before = _snapshot(state_dir)
    view = read_durable_state(state_dir)
    recorded = _configured(view)
    export_run_artifacts(
        state_dir,
        recorded.run.run_id,
        out,
        strategy_spec=_tsmom(),
        cost_model=fx.COST_MODEL,
        label_spec=fx.LABEL_SPEC,
    )
    assert _snapshot(state_dir) == before


# ---- exporting ----------------------------------------------------------------------------


def test_a_run_is_exported_with_its_report_spec_cost_and_label(
    state_dir: Path, view: DurableStateView, out: Path
) -> None:
    recorded = _configured(view)
    files = export_run_artifacts(
        state_dir,
        recorded.run.run_id,
        out,
        strategy_spec=_tsmom(),
        cost_model=fx.COST_MODEL,
        label_spec=fx.LABEL_SPEC,
    )
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [
            EXPERIMENT_RUN_FILE,
            VALIDATION_REPORT_FILE,
            STRATEGY_SPEC_FILE,
            COST_MODEL_FILE,
            LABEL_SPEC_FILE,
        ]
    )

    def read(path: Path | None) -> object:
        assert path is not None and path.parent == out
        return json.loads(path.read_text(encoding="utf-8"))

    run = ExperimentRun.model_validate(read(files.run))
    assert run.content_hash() == recorded.run.content_hash()
    report = ValidationReport.model_validate(read(files.report))
    assert report.content_hash() == recorded.reports[0].content_hash()
    spec = StrategySpec.model_validate(read(files.strategy_spec))
    assert spec.content_hash() == recorded.strategy_hash == _tsmom().content_hash()
    cost = CostModelSpec.model_validate(read(files.cost_model))
    assert cost.content_hash() == fx.COST_MODEL.content_hash()
    label = OutcomeLabelSpec.model_validate(read(files.label_spec))
    assert label.content_hash() == fx.LABEL_SPEC.content_hash()


def test_cost_and_label_are_exported_only_when_given(
    state_dir: Path, view: DurableStateView, out: Path
) -> None:
    files = export_run_artifacts(
        state_dir, _configured(view).run.run_id, out, strategy_spec=_tsmom()
    )
    assert files.cost_model is None and files.label_spec is None
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [EXPERIMENT_RUN_FILE, VALIDATION_REPORT_FILE, STRATEGY_SPEC_FILE]
    )


def test_an_evolved_strategy_spec_comes_from_the_state(
    state_dir: Path, view: DurableStateView, out: Path
) -> None:
    evolved = [item for item in view.runs if item.strategy in view.strategies]
    if not evolved:
        pytest.fail("the TEST ONLY loop is expected to trial an evolved strategy in round 1")
    recorded = evolved[0]
    files = export_run_artifacts(state_dir, recorded.run.run_id, out)
    assert files.strategy_spec is not None
    spec = StrategySpec.model_validate(json.loads(files.strategy_spec.read_text("utf-8")))
    assert spec.content_hash() == recorded.strategy_hash
    # a different spec for the same ref is refused rather than silently replaced
    with pytest.raises(StateExportRefused, match="the state holds"):
        export_run_artifacts(state_dir, recorded.run.run_id, out, strategy_spec=_tsmom())


# ---- refusals -----------------------------------------------------------------------------


def test_a_configured_strategy_needs_its_exact_spec(
    state_dir: Path, view: DurableStateView, out: Path
) -> None:
    run_id = _configured(view).run.run_id
    with pytest.raises(StateExportRefused, match="pass its StrategySpec"):
        export_run_artifacts(state_dir, run_id, out)
    other = StrategySpec.model_validate(
        {**_tsmom().model_dump(mode="json"), "param_search_space": {"lookback": [60, 240]}}
    )
    assert other.content_hash() != _tsmom().content_hash()
    with pytest.raises(StateExportRefused, match="is not the"):
        export_run_artifacts(state_dir, run_id, out, strategy_spec=other)
    assert list(out.iterdir()) == []


def test_a_cost_model_or_label_spec_the_state_did_not_bind_is_refused(
    state_dir: Path, view: DurableStateView, out: Path
) -> None:
    run_id = _configured(view).run.run_id
    other_cost = fx.COST_MODEL.model_copy(
        update={"fee_rate_per_side": fx.COST_MODEL.fee_rate_per_side * 2}
    )
    with pytest.raises(StateExportRefused, match="CostModelSpec"):
        export_run_artifacts(state_dir, run_id, out, strategy_spec=_tsmom(), cost_model=other_cost)
    other_label = OutcomeLabelSpec.bind(
        OutcomeSpec(
            name="fwd_2h",
            version="1.0.0",
            created_at=fx.T0,
            horizon=2 * timedelta(hours=1),
            label_definition="2h fwd (TEST ONLY)",
        ),
        OutcomeMethod.FORWARD_RETURN,
    )
    with pytest.raises(StateExportRefused, match="OutcomeLabelSpec"):
        export_run_artifacts(state_dir, run_id, out, strategy_spec=_tsmom(), label_spec=other_label)
    assert list(out.iterdir()) == []


def test_an_unknown_run_is_refused(state_dir: Path, out: Path) -> None:
    with pytest.raises(StateExportRefused, match="no recorded run"):
        export_run_artifacts(state_dir, "no-such-run", out)


def test_nothing_is_overwritten(state_dir: Path, view: DurableStateView, out: Path) -> None:
    run_id = _configured(view).run.run_id
    (out / STRATEGY_SPEC_FILE).write_text("keep me", encoding="utf-8")
    with pytest.raises(StateExportRefused, match="nothing is overwritten"):
        export_run_artifacts(state_dir, run_id, out, strategy_spec=_tsmom())
    assert sorted(path.name for path in out.iterdir()) == [STRATEGY_SPEC_FILE]
    assert (out / STRATEGY_SPEC_FILE).read_text(encoding="utf-8") == "keep me"


def test_the_output_must_lie_outside_the_state_and_exist(
    state_dir: Path, view: DurableStateView, tmp_path: Path
) -> None:
    run_id = _configured(view).run.run_id
    with pytest.raises(StateExportRefused, match="outside the state"):
        export_run_artifacts(state_dir, run_id, state_dir, strategy_spec=_tsmom())
    with pytest.raises(StateExportRefused, match="existing directory"):
        export_run_artifacts(state_dir, run_id, tmp_path / "missing", strategy_spec=_tsmom())


def test_only_audited_rounds_count(state_dir: Path, tmp_path: Path) -> None:
    """A checkpoint whose round the audit never recorded (the crash window) is not exported."""
    copy = tmp_path / "state"
    shutil.copytree(state_dir, copy)
    audit = copy / AUDIT_FILE
    lines = audit.read_text(encoding="utf-8").splitlines(keepends=True)
    assert json.loads(lines[-1])["type"] == "loop_round_recorded"
    audit.write_text("".join(lines[:-1]), encoding="utf-8")  # round 1 started, never recorded
    truncated = read_durable_state(copy)
    assert truncated.rounds == ROUNDS - 1
    assert {item.round_index for item in truncated.runs} == {0}


def test_a_corrupted_or_foreign_state_is_refused(state_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / "state"
    shutil.copytree(state_dir, copy)
    memory = copy / MEMORY_FILE
    data = bytearray(memory.read_bytes())
    data[len(data) // 2] ^= 0x01
    memory.write_bytes(bytes(data))
    with pytest.raises(StateExportRefused):
        read_durable_state(copy)
    with pytest.raises(StateExportRefused, match="not a loop state directory"):
        read_durable_state(tmp_path / "nowhere")
