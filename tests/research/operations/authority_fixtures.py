"""TEST ONLY fixtures for the ADR-0098 / ADR-0100 authority tests (ADR-0105 §7).

Shared building blocks of the resolver tests (``research.operations.authority``): a real
``LifecycleRegistry`` written in a temporary directory, a baseline ``ExperimentRun`` that carries
an ADR-0100 修订 2 ``hlens.p11.inputs@1.0.0`` record, a declared ``WindowTargetSource`` returning
fixed targets (caller code by design: the resolver binds its declared identity and checks every
target, it never re-derives them) and a real ``BarBacktester``. Every number — prices, equity,
the decision grid, the degradation threshold — is fabricated and arbitrary; nothing here is
evidence about a strategy or a Profile calibration.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.cost_model import CostModelSpec
from core.contracts.strategy import PriceBar, TargetPosition
from core.contracts.validation_profile import (
    CostStressParams,
    LifecycleParams,
    ValidationProfile,
)
from core.domain.base import Ref
from core.domain.research import ExperimentRun, GateResult, ValidationReport, Verdict
from core.domain.specs import DatasetRef
from core.lifecycle.strategy import (
    HUMAN_APPROVAL_TRANSITIONS,
    LifecycleState,
    LifecycleTransition,
)
from infrastructure.registry import ProfileFreezeRegistry
from infrastructure.registry.lifecycle import LifecycleHead, LifecycleRegistry
from plugins.backtest import BarBacktester
from research.experiments.run_inputs import RunInputs, with_run_inputs
from research.loop.dataset_source import DatasetCatalog
from research.operations.authority import (
    AuthorityEnvironment,
    AuthorityRefused,
    AuthorityResolution,
    TargetSourceIdentity,
    WindowExecution,
    resolve_degradation_inputs,
)
from research.operations.degradation import BaselineMetricSet, ObservationWindow
from tests import factories
from tests.promotion.fixtures import (
    TOY_FREEZE_APPROVER,
    TOY_FREEZE_TIME,
    toy_calibration_report,
    toy_profile,
)
from tests.research.operations.fixtures import write_json

S = LifecycleState
MINUTE = timedelta(minutes=1)
#: The bar latency of the synthetic bars (``available_time = interval_end + LATENCY``).
LATENCY = timedelta(seconds=5)
SUBJECT = factories.strategy_ref()
OTHER_SUBJECT = factories.strategy_ref(name="s_other")
#: The legal path IDEA → ACTIVE (ADR-0006).
PATH_TO_ACTIVE: tuple[LifecycleState, ...] = (
    S.IDEA,
    S.CANDIDATE,
    S.VALIDATION,
    S.OOS,
    S.PAPER,
    S.PRODUCTION_CANDIDATE,
    S.ACTIVE,
)
#: TEST ONLY cost model (arbitrary rates).
COST_MODEL = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    fee_rate_per_side=Decimal("0.0004"),
    slippage_rate_per_side=Decimal("0.0001"),
)
#: TEST ONLY strategy spec hash and declared strategy plugin (identities only, no provider runs).
STRATEGY_SPEC_HASH = "5" * 64
STRATEGY_PLUGIN = ("test_only_strategy@1.0.0", "6" * 64)
INITIAL_EQUITY = Decimal("10000")
#: TEST ONLY strategy params (the baseline run's ``repro.params`` besides the run inputs record).
PARAMS: Mapping[str, str | int | float | bool] = {"lookback": 2}
#: The registry metric of the cases: ``breakeven_cost_multiple`` @ ``G4.cost_stress.breakeven``.
METRIC = "breakeven_cost_multiple"
GATE_ID = "G4.cost_stress.breakeven"
GATE_LABEL = "breakeven_cost_multiple[>=]"


def run_inputs(**overrides: Any) -> RunInputs:
    """A TEST ONLY ``hlens.p11.inputs@1.0.0`` record (one-minute grid, no warm-up)."""
    values: dict[str, Any] = {
        "decision_step": MINUTE,
        "decision_warmup": timedelta(0),
        "initial_equity": INITIAL_EQUITY,
        "family_trial_count": 1,
        "validation_seed": 7,
        "control_seeds": None,
        "cscv_partitions": None,
        "impact_coefficient": None,
        "state_labeller": None,
    }
    values.update(overrides)
    return RunInputs(**values)


def exact_cost_stress() -> CostStressParams:
    """The factory Profile's TEST ONLY ``cost_stress`` with its ADR-0052 exact siblings (each
    derived from the float value; no new number), so the G4 cost-stress gates carry exact
    values."""
    base = toy_profile().cost_stress
    return CostStressParams.model_validate(
        {
            **base.model_dump(),
            "stress_multipliers_exact": tuple(Decimal(repr(v)) for v in base.stress_multipliers),
            "min_breakeven_cost_multiple_exact": Decimal(repr(base.min_breakeven_cost_multiple)),
        }
    )


def profile(
    thresholds: Mapping[str, str] | None = None, *, calibration_report: str | None = None
) -> ValidationProfile:
    """The FROZEN TEST ONLY Profile with exact degradation thresholds (default: one, arbitrary)
    and the exact siblings of its cost-stress thresholds (``exact_cost_stress``);
    ``calibration_report``: the cited calibration report hash (default: ``toy_profile``'s)."""
    exact = {METRIC: "0.5"} if thresholds is None else thresholds
    lifecycle = LifecycleParams.model_validate(
        {
            "paper_period": toy_profile().lifecycle.paper_period,
            "paper_acceptance_rule": "test-rule",
            "degradation_thresholds": {key: float(Decimal(v)) for key, v in exact.items()},
            "degradation_thresholds_exact": {key: Decimal(v) for key, v in exact.items()},
        }
    )
    if calibration_report is None:
        return toy_profile(lifecycle=lifecycle, cost_stress=exact_cost_stress())
    return toy_profile(
        calibration_report=calibration_report, lifecycle=lifecycle, cost_stress=exact_cost_stress()
    )


def baseline_run(
    bound: ValidationProfile,
    *,
    inputs: RunInputs | None = None,
    record: bool = True,
    params: Mapping[str, str | int | float | bool] = PARAMS,
    dataset_snapshots: Sequence[DatasetRef] | None = None,
    plugin_versions: Mapping[str, str] | None = None,
    run_id: str = "run-authority-1",
) -> ExperimentRun:
    """The baseline run of ``SUBJECT`` under ``bound``: the real ``BarBacktester`` and the TEST
    ONLY strategy plugin in ``plugin_versions``, ``COST_MODEL`` bound by hash and (``record``) the
    run inputs record in its hashed ``repro.params``."""
    deps = factories.dependency_hashes(factories.hypothesis_ref(), factories.outcome_ref(), SUBJECT)
    deps[str(SUBJECT)] = STRATEGY_SPEC_HASH
    deps[str(COST_MODEL.ref)] = COST_MODEL.content_hash()
    backtester = BarBacktester().descriptor
    plugins = (
        {backtester.plugin_key: backtester.content_hash(), STRATEGY_PLUGIN[0]: STRATEGY_PLUGIN[1]}
        if plugin_versions is None
        else dict(plugin_versions)
    )
    run_params: dict[str, str | int | float | bool] = dict(params)
    if record:
        run_params = with_run_inputs(run_params, inputs or run_inputs())
    repro = factories.repro_tuple(
        strategy_ref=SUBJECT,
        risk_policy_ref=None,
        cost_model_ref=COST_MODEL.ref,
        dependency_hashes=deps,
        plugin_versions=plugins,
        params=run_params,
        seeds=(0, 7),
        validation_profile=bound.ref,
        validation_profile_hash=bound.content_hash(),
        **({} if dataset_snapshots is None else {"dataset_snapshots": tuple(dataset_snapshots)}),
    )
    return factories.experiment_run(run_id=run_id, repro=repro)


def gate(value: str, *, gate_id: str = GATE_ID, label: str = GATE_LABEL) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        metric=label,
        value=float(Decimal(value)),
        value_exact=Decimal(value),
        verdict=Verdict.PASS,
    )


def report_of(
    bound: ValidationProfile,
    run: ExperimentRun,
    *,
    gates: Sequence[GateResult] | None = None,
    subject: Ref = SUBJECT,
) -> ValidationReport:
    """The PASS baseline report of ``run`` (default: one ``G4.cost_stress.breakeven`` gate)."""
    return factories.validation_report(
        Verdict.PASS,
        report_id="rep-authority-1",
        run_id=run.run_id,
        subject=subject,
        experiment_hash=run.experiment_hash,
        validation_profile=bound.ref,
        validation_profile_hash=bound.content_hash(),
        gates=tuple(gates) if gates is not None else (gate("3.5"),),
    )


def baseline_set(report: ValidationReport, gate_ids: Mapping[str, str]) -> BaselineMetricSet:
    """The baseline set citing ``gate_ids`` (metric → gate id) with the report's exact values."""
    by_id = {item.gate_id: item for item in report.gates}
    metrics: dict[str, Decimal] = {}
    for metric, gate_id in gate_ids.items():
        exact = by_id[gate_id].value_exact if gate_id in by_id else Decimal("1")
        metrics[metric] = exact if exact is not None else Decimal("1")
    return BaselineMetricSet(
        validation_report_hash=report.content_hash(),
        metrics=metrics,
        gate_ids=dict(gate_ids),
    )


def bars(
    instruments: Sequence[str],
    start: datetime,
    count: int,
    *,
    base: str = "100",
    step: str = "1.25",
) -> tuple[PriceBar, ...]:
    """``count`` contiguous one-minute bars per instrument from ``start`` (a rising path)."""
    out: list[PriceBar] = []
    for offset, instrument in enumerate(instruments):
        for index in range(count):
            opened = Decimal(base) + offset * 10 + index * Decimal(step)
            closed = opened + Decimal(step)
            begin = start + index * MINUTE
            out.append(
                PriceBar(
                    instrument=instrument,
                    interval_start=begin,
                    interval_end=begin + MINUTE,
                    available_time=begin + MINUTE + LATENCY,
                    open=opened,
                    high=closed + 1,
                    low=opened - 1,
                    close=closed,
                )
            )
    return tuple(sorted(out, key=lambda bar: (bar.interval_start, bar.instrument)))


def long_targets(bars_: Sequence[PriceBar], weight: str = "0.5") -> tuple[TargetPosition, ...]:
    """One long target per instrument at the first decision after its first bar is known (an
    arbitrary TEST ONLY position; never a strategy)."""
    out: list[TargetPosition] = []
    for instrument in sorted({bar.instrument for bar in bars_}):
        first = min(
            (bar for bar in bars_ if bar.instrument == instrument),
            key=lambda bar: bar.interval_start,
        )
        known = first.available_time
        decision = first.interval_end + MINUTE  # a grid point after the first bar is available
        out.append(
            TargetPosition(
                decision_time=decision,
                instrument=instrument,
                target_weight=Decimal(weight),
                inputs_used=1,
                latest_input_available_time=known,
            )
        )
    return tuple(sorted(out, key=lambda item: (item.decision_time, item.instrument)))


def identity(
    run: ExperimentRun, instruments: Sequence[str], **overrides: Any
) -> TargetSourceIdentity:
    """The target source identity the baseline ``run`` recorded (``overrides`` change one)."""
    values: dict[str, Any] = {
        "strategy_ref": SUBJECT,
        "strategy_spec_hash": STRATEGY_SPEC_HASH,
        "risk_policy_ref": None,
        "risk_policy_hash": None,
        "params": dict(PARAMS),
        "plugins": {STRATEGY_PLUGIN[0]: STRATEGY_PLUGIN[1]},
        "instruments": tuple(instruments),
        "decision_step": MINUTE,
        "decision_warmup": timedelta(0),
        "initial_equity": INITIAL_EQUITY,
    }
    values.update(overrides)
    del run  # the identity is declared, not derived from the run
    return TargetSourceIdentity(**values)


@dataclass(frozen=True)
class DeclaredTargets:
    """A TEST ONLY ``WindowTargetSource``: a declared identity and fixed targets (or a refusal).
    It records the windows it was asked about, so a test can see it ran (or did not)."""

    identity: TargetSourceIdentity
    positions: tuple[TargetPosition, ...] = ()
    refusal: str | None = None
    calls: list[ObservationWindow] | None = None

    def targets(
        self, bars_: tuple[PriceBar, ...], window: ObservationWindow
    ) -> Sequence[TargetPosition]:
        if self.calls is not None:
            self.calls.append(window)
        if self.refusal is not None:
            raise ValueError(self.refusal)
        return self.positions


def execution(
    run: ExperimentRun,
    instruments: Sequence[str],
    positions: Sequence[TargetPosition] = (),
    **overrides: Any,
) -> WindowExecution:
    """The real ``BarBacktester``, ``COST_MODEL`` and the declared targets of ``run``."""
    values: dict[str, Any] = {
        "backtester": BarBacktester(),
        "cost_model": COST_MODEL,
        "initial_equity": INITIAL_EQUITY,
        "targets": DeclaredTargets(identity(run, instruments), tuple(positions)),
    }
    values.update(overrides)
    return WindowExecution(**values)


# ---- the Lifecycle Registry ---------------------------------------------------------------


def transitions(
    subject: Ref, path: Sequence[LifecycleState], start: datetime, *, step: timedelta = MINUTE
) -> tuple[LifecycleTransition, ...]:
    """The legal transitions along ``path`` from ``start`` (one ``step`` apart)."""
    return tuple(
        LifecycleTransition(
            subject=subject,
            from_state=a,
            to_state=b,
            reason="TEST ONLY transition",
            evidence=("TEST-ONLY-evidence",),
            triggered_by="test",
            approved_by="TEST-ONLY-human" if (a, b) in HUMAN_APPROVAL_TRANSITIONS else None,
            occurred_at=start + index * step,
        )
        for index, (a, b) in enumerate(zip(path, path[1:], strict=False))
    )


def write_registry(
    root: Path,
    records: Sequence[LifecycleTransition],
    *,
    anchor: Path | None = None,
) -> list[LifecycleHead]:
    """Append ``records`` (in order) to a writer instance at ``root``; returns every head."""
    heads: list[LifecycleHead] = []
    with LifecycleRegistry(root, anchor=anchor) as registry:
        head = registry.head
        heads.append(head)
        for record in records:
            head = registry.append(record, expected_head=head)
            heads.append(head)
    return heads


# ---- a trusted ``--authority-environment`` factory for the CLI tests ------------------------

#: What ``environment_factory`` returns (set by a test, through ``monkeypatch``): the
#: ``AuthorityEnvironment`` a deployment factory would build, or any object to see it refused.
FACTORY_ENVIRONMENT: list[object] = []


def environment_factory() -> object:
    """A TEST ONLY ``MODULE:CALLABLE`` factory: the environment a test put in
    ``FACTORY_ENVIRONMENT`` (``degradation_cli`` refuses anything but an
    ``AuthorityEnvironment``)."""
    return FACTORY_ENVIRONMENT[-1]


@contextmanager
def environment_context() -> Iterator[object]:
    """The same as a context manager (held open until the report is written); records exit."""
    try:
        yield FACTORY_ENVIRONMENT[-1]
    finally:
        FACTORY_ENVIRONMENT.append("exited")


def failing_factory() -> object:
    """A TEST ONLY factory that fails (its message must not reach the CLI's output)."""
    raise RuntimeError("TEST ONLY secret detail")


# ---- a whole resolver case ------------------------------------------------------------------

#: The two instruments of the v3 world (``test_authority_resolver``): twelve one-minute bars each,
#: 22:05 – 22:17 of 2023-11-14.
INSTRUMENTS = ("BTC-USDT", "ETH-USDT")
START = datetime(2023, 11, 14, 22, 5, tzinfo=UTC)
BAR_COUNT = 12
WINDOW = ObservationWindow(
    start=START, end=START + BAR_COUNT * MINUTE, label="TEST ONLY authority window"
)
AS_OF = datetime(2023, 11, 15, tzinfo=UTC)  # after the last bar's availability
ACTIVE_FROM = datetime(2023, 11, 14, 12, tzinfo=UTC)  # the first lifecycle transition


@dataclass(frozen=True)
class Source:
    """The pinned v3 source of a case: the catalog, the dataset id and manifest, and the
    ``DatasetRef`` the baseline run read."""

    catalog: DatasetCatalog
    dataset_id: str
    manifest_hash: str
    dataset: DatasetRef


@dataclass
class ResolverCase:
    """One authority-mode case (files written for the CLI as well): a FROZEN Profile in a
    temporary freeze registry, a PASS report of a baseline run, its baseline set and a real
    Lifecycle Registry with its external anchor. ``calls``: the windows the declared pipeline
    was asked about (cleared by ``refused``)."""

    source: Source
    root: Path
    profile: ValidationProfile
    run: ExperimentRun
    report: ValidationReport
    baseline: BaselineMetricSet
    registry: Path
    anchor: Path
    freezes: Path
    freeze_anchor: Path
    calls: list[ObservationWindow]

    def execution(self, **changes: Any) -> WindowExecution:
        instruments = changes.pop("instruments", INSTRUMENTS)
        positions = long_targets(bars(INSTRUMENTS, START, BAR_COUNT))
        targets = DeclaredTargets(
            identity(self.run, instruments, **changes), positions, calls=self.calls
        )
        return execution(self.run, INSTRUMENTS, targets=targets)

    def environment(self, **changes: Any) -> AuthorityEnvironment:
        return AuthorityEnvironment(
            catalog=changes.pop("catalog", self.source.catalog),
            execution=changes.pop("execution", self.execution()),
            baseline_run=changes.pop("baseline_run", self.run),
            baseline_manifest_hash=changes.pop("baseline_manifest_hash", self.source.manifest_hash),
        )

    def resolve(self, *, anchored: bool = False, **overrides: Any) -> AuthorityResolution:
        with LifecycleRegistry.open_snapshot(
            self.registry, anchor=self.anchor if anchored else None
        ) as snapshot:
            kwargs: dict[str, Any] = {
                "subject": SUBJECT,
                "lifecycle": snapshot,
                "head": snapshot.head,
                "profile": self.profile,
                "baseline_report": self.report,
                "baseline": self.baseline,
                "baseline_run": self.run,
                "baseline_manifest_hash": self.source.manifest_hash,
                "catalog": self.source.catalog,
                "dataset_id": self.source.dataset_id,
                "manifest_hash": self.source.manifest_hash,
                "execution": self.execution(),
                "window": WINDOW,
                "as_of": AS_OF,
            }
            kwargs.update(overrides)
            return resolve_degradation_inputs(**kwargs)

    def refused(self, code: str, **overrides: Any) -> str:
        self.calls.clear()
        with pytest.raises(AuthorityRefused) as refused:
            self.resolve(**overrides)
        assert refused.value.code == code, str(refused.value)
        return str(refused.value)

    def cli_args(self, reports: Path, **changes: str) -> list[str]:
        files = {
            "profile": write_json(self.root / "profile.json", self.profile.model_dump(mode="json")),
            "report": write_json(self.root / "report.json", self.report.model_dump(mode="json")),
            "baseline": write_json(self.root / "baseline.json", self.baseline.payload()),
        }
        values = {
            "--subject": str(SUBJECT),
            "--profile": str(files["profile"]),
            "--baseline-report": str(files["report"]),
            "--baseline-set": str(files["baseline"]),
            "--authority-registry": str(self.registry),
            "--authority-head": "latest",
            "--dataset-id": self.source.dataset_id,
            "--manifest-hash": self.source.manifest_hash,
            "--authority-as-of": "2023-11-15T00:00:00Z",
            "--window-start": "2023-11-14T22:05:00Z",
            "--window-end": "2023-11-14T22:17:00Z",
            "--window-label": WINDOW.label,
            "--freeze-registry": str(self.freezes),
            "--freeze-anchor": str(self.freeze_anchor),
            "--reports-root": str(reports),
        }
        values.update(changes)
        return [item for key, value in values.items() if value != "" for item in (key, value)]


def resolver_case(
    source: Source,
    root: Path,
    *,
    thresholds: dict[str, str] | None = None,
    gates: tuple[GateResult, ...] | None = None,
    gate_ids: dict[str, str] | None = None,
    record: bool = True,
    lifecycle_path: tuple[LifecycleState, ...] = PATH_TO_ACTIVE,
    lifecycle_start: datetime = ACTIVE_FROM,
    dataset_snapshots: tuple[DatasetRef, ...] | None = None,
) -> ResolverCase:
    """Write one case under ``root`` (module docs of ``ResolverCase``)."""
    root.mkdir(parents=True, exist_ok=True)
    report_bytes, report_hash = toy_calibration_report()
    bound = profile(thresholds, calibration_report=report_hash)
    freezes, freeze_anchor = root / "freezes", root / "freezes.anchor.jsonl"
    with ProfileFreezeRegistry(freezes, anchor=freeze_anchor) as registry:
        registry.register_freeze(
            bound, report_bytes, approved_by=TOY_FREEZE_APPROVER, approved_at=TOY_FREEZE_TIME
        )
    run = baseline_run(
        bound,
        record=record,
        dataset_snapshots=(source.dataset,) if dataset_snapshots is None else dataset_snapshots,
    )
    report = report_of(bound, run, gates=gates)
    baseline = baseline_set(report, gate_ids or {METRIC: GATE_ID})
    registry_root, anchor = root / "lifecycle", root / "lifecycle.anchor.jsonl"
    write_registry(
        registry_root,
        transitions(SUBJECT, lifecycle_path, lifecycle_start, step=timedelta(hours=1)),
        anchor=anchor,
    )
    return ResolverCase(
        source=source,
        root=root,
        profile=bound,
        run=run,
        report=report,
        baseline=baseline,
        registry=registry_root,
        anchor=anchor,
        freezes=freezes,
        freeze_anchor=freeze_anchor,
        calls=[],
    )


def verifier_factory(adapter: object, storage: object) -> object:
    """A TEST ONLY ``--authority-evidence-verifier`` factory ``(adapter, storage) -> object``:
    whatever a test put in ``FACTORY_ENVIRONMENT`` (never a real verifier)."""
    del adapter, storage
    return FACTORY_ENVIRONMENT[-1]
