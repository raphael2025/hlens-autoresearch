"""Authoritative inputs for the P11 degradation check (ADR-0098; supersedes ADR-0080's block).

ADR-0067's ``run_degradation_check`` takes a caller-declared ``LifecycleHistory`` and a
caller-declared ``RecentMetricManifest`` and says so in its evidence. This module is the ADR-0098
§4 resolver: ``resolve_degradation_inputs`` derives both from three authorities and records where
they came from in an ``AuthorityProvenance`` that ``run_degradation_check(..., authority=...)``
writes into the report's ``evidence.authority``. The explicit caller-declared path is unchanged and
carries no ``authority`` field — that absence is how the two evidence strengths stay apart.

**1. Lifecycle (ADR-0098 §1).** ``infrastructure.registry.lifecycle.LifecycleRegistry`` at an
**explicit** head (the latest ``registry.head`` or one the caller pinned; a head not on the chain is
``lifecycle_head_unknown``). The subject must be in the ACTIVE set at that head
(``lifecycle_not_active`` otherwise); its replayed history is the ``LifecycleHistory`` handed to the
operation, and the head identity plus the replayed record hashes go into the provenance, with
``anchor`` = ``present`` when the registry was opened with its external anchor (a rollback of whole
trailing records would have been refused on open) or ``absent`` (not detectable; recorded, not
refused — ADR-0098 修订 1).

**2. Source (ADR-0098 §2).** The only non-synthetic source is one ADR-0077 v3
``ResearchDatasetEvidenceManifest``, read through the ``DatasetCatalog`` (its ``evidence_verifier``
is required). ``dataset_id`` is the manifest's ``selection_id`` — the identity the v3 store keys a
dataset by (one manifest per selection, ADR-0052 V7) — and the source identity is
``(dataset_id, manifest_content_hash)`` plus the manifest's snapshot, policy, rule and universe
bindings. Rules, each a named refusal:

- every v3 manifest row persisted under ``dataset_id`` is read (the evidence table at its current
  head — a superset of the as-of state, so a conflicting row persisted later is also refused):
  more than one content hash → ``source_conflict`` (never "pick the latest"); none →
  ``source_unavailable``; the declared ``manifest_hash`` not among them →
  ``source_identity_mismatch``;
- the manifest loads and verifies through the builder's own store (``load_any_manifest``) and is
  v3; anything else → ``source_unavailable``;
- scope: the dataset the subject's validation bound must be the same universe, bar specification
  and representation as the source → ``source_scope_mismatch`` otherwise. The baseline dataset is
  the caller-named ``baseline_manifest_hash`` whose ``DatasetRef`` must be one of the baseline
  ``ExperimentRun``'s ``repro.dataset_snapshots`` (the run whose ``run_id`` / ``experiment_hash``
  the PASS report cites). Compared: ``universe_spec``; ``data_type`` (bar specification; a v2
  baseline records none, so it is refused rather than assumed); ``dataset.table`` and the PIT
  ``point_in_time_binding`` / ``availability_bindings`` / ``precedence_bindings`` /
  ``parser_bindings`` (representation — the same fields the sealed-pair rule of
  ``research.loop.dataset_source`` compares across windows);
- only ``available_time <= window.end`` (``backtest_bars_from_dataset(price_cutoff=window.end)``
  refuses a later bar instead of dropping it) over the bars whose ``interval_start`` is in
  ``[window.start, window.end)``; per instrument the bars must tile the window exactly (first
  starts at ``window.start``, each starts where the previous ended, the last ends at
  ``window.end``). A missing bar, a late bar or a window the manifest does not cover →
  ``source_incomplete``; nothing is filled or interpolated.

**3. Metrics (ADR-0098 §3).** ``MONITORING_METRICS`` is the closed registry. Each
``MonitoringMetricDefinition`` names the baseline gate whose value it reproduces and calls **the
validation function that produced that gate** on the window's return series, in the same return
construction (``research.validation.returns.from_backtest``) and under the same cost model:

- ``breakeven_cost_multiple`` → ``research.validation.robustness.cost_stress_check`` and its gate
  ``G4.cost_stress.breakeven`` (``PeriodReturns.breakeven_cost_multiple``, quantized by
  ``compare_gate`` under ``GATE_VALUE_QUANTIZATION``). When that function reports its own
  insufficient-evidence gate (``no_cost_no_trades``) the metric is **missing**, as in validation.

Every other Profile degradation metric is ``metric_undefined``: the in-sample G2 / G3 / G5 metrics
are computed from Outcome labels and walk-forward folds, the other G4 metrics from parameter
families, re-runs or the Profile's fixed walk-forward windows — none is a function of one
observation window's return series, and no formula is invented for them. The caller's baseline set
must cite, for each metric, exactly the definition's gate (``baseline_binding_mismatch``).

**Returns (H7).** The window's series is one backtest through the ``BacktestProvider`` protocol:
the caller supplies a ``WindowExecution`` — the provider, the bound ``CostModelSpec``, the initial
equity and a ``WindowTargetSource`` (the admitted strategy's decision pipeline over the window's
proven bars). Its declared identities must equal the baseline run's reproducibility tuple
(backtest provider descriptor hash in ``plugin_versions``; cost model ref + content hash in
``dependency_hashes``; strategy ref + spec hash, risk policy ref + hash, params and every declared
plugin hash) — else ``execution_mismatch``. The provider's result is re-validated and checked with
``BacktestResult.check_answers``; one window yields one sample.

**Boundaries.** Nothing here reads a clock (the as-of time is ``window.end``), schedules, loops,
writes the lifecycle, or writes a report; any refusal raises ``AuthorityRefused`` (a
``DegradationOperationRefused`` with a ``code``) before the operation runs, so no report is
written. The ADR-0074 operator and the API do not import this module (ADR-0098 §4).

**Honest boundary.** The decision pipeline (``WindowTargetSource``) is caller code bound by its
declared identities, not re-derived here; the Lifecycle Registry's actors are declared names. The
command line cannot construct the catalog or the decision pipeline itself, so
``degradation_cli`` refuses the authority mode unless a deployment-trusted
``--authority-environment MODULE:CALLABLE`` factory or an embedding caller supplies an
``AuthorityEnvironment`` (ADR-0098 修订 1; see that module).

Code completion (2026-09-30, CODE_COMPLETE / DEBUG_PENDING; not run, not tested).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import ValidationError
from pyiceberg.expressions import EqualTo

from apps.worker.degradation import DegradationMonitor
from core.contracts.catalog import TableNotFound
from core.contracts.cost_model import CostModelSpec
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    TargetPosition,
)
from core.contracts.universe import DATASET_SELECTION_ID_PATTERN, ResearchDatasetEvidenceManifest
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref, canonical_json, content_hash
from core.domain.research import ExperimentRun, ValidationReport
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from infrastructure.bars.dataset import (
    DatasetBarsError,
    DatasetPriceBars,
    backtest_bars_from_dataset,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_EVIDENCE_MANIFESTS
from infrastructure.dataset.manifests import ManifestFormError
from infrastructure.feature.dataset import (
    AnyDatasetManifest,
    DatasetBindingError,
    load_any_manifest,
)
from infrastructure.registry.lifecycle import LifecycleHead, LifecycleRegistry, UnknownHead
from infrastructure.registry.registry import RegistryError
from research.loop.dataset_source import DatasetCatalog
from research.operations.degradation import (
    BaselineMetricSet,
    DegradationOperationRefused,
    ObservationSource,
    ObservationWindow,
    RecentMetricManifest,
    RecentMetricSet,
)
from research.validation.gates import GATE_VALUE_QUANTIZATION
from research.validation.returns import PeriodReturns, from_backtest
from research.validation.robustness import cost_stress_check

__all__ = [
    "ANCHOR_ABSENT",
    "ANCHOR_PRESENT",
    "AUTHORITY_FORMAT",
    "BASELINE_BINDING_MISMATCH",
    "EXECUTION_MISMATCH",
    "LIFECYCLE_HEAD_UNKNOWN",
    "LIFECYCLE_NOT_ACTIVE",
    "LIFECYCLE_UNAVAILABLE",
    "METRIC_REFUSED",
    "METRIC_REGISTRY_ID",
    "METRIC_UNDEFINED",
    "MONITORING_METRICS",
    "SOURCE_CONFLICT",
    "SOURCE_IDENTITY_MISMATCH",
    "SOURCE_INCOMPLETE",
    "SOURCE_SCOPE_MISMATCH",
    "SOURCE_UNAVAILABLE",
    "AuthorityEnvironment",
    "AuthorityProvenance",
    "AuthorityRefused",
    "AuthorityResolution",
    "MonitoringMetricDefinition",
    "SourceIdentity",
    "TargetSourceIdentity",
    "WindowExecution",
    "WindowTargetSource",
    "resolve_degradation_inputs",
]

#: Identity of the provenance payload (part of the report's hash-bound evidence).
AUTHORITY_FORMAT: Final = "hlens.p11.authority-provenance@1.0.0"
#: ``authority.lifecycle.anchor``: the Lifecycle Registry was verified against an external anchor
#: (``present``) or opened without one (``absent``: a rollback of whole trailing records cannot be
#: detected; ADR-0098 修订 1 — recorded, not refused).
ANCHOR_PRESENT: Final = "present"
ANCHOR_ABSENT: Final = "absent"
#: Identity and version of the closed metric registry below (the recent manifest's ``method_id``).
METRIC_REGISTRY_ID: Final = "hlens.p11.monitoring-metrics@1.0.0"

# ---- refusal codes (ADR-0098 names the first four; the rest name the other fail-closed cases) --
METRIC_UNDEFINED: Final = "metric_undefined"
SOURCE_SCOPE_MISMATCH: Final = "source_scope_mismatch"
SOURCE_CONFLICT: Final = "source_conflict"
SOURCE_INCOMPLETE: Final = "source_incomplete"
SOURCE_UNAVAILABLE: Final = "source_unavailable"
SOURCE_IDENTITY_MISMATCH: Final = "source_identity_mismatch"
LIFECYCLE_HEAD_UNKNOWN: Final = "lifecycle_head_unknown"
LIFECYCLE_NOT_ACTIVE: Final = "lifecycle_not_active"
LIFECYCLE_UNAVAILABLE: Final = "lifecycle_unavailable"
BASELINE_BINDING_MISMATCH: Final = "baseline_binding_mismatch"
EXECUTION_MISMATCH: Final = "execution_mismatch"
METRIC_REFUSED: Final = "metric_refused"

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SELECTION_ID: Final = re.compile(DATASET_SELECTION_ID_PATTERN)
#: PIT bindings that fix how Canonical rows become the dataset's representation (module docs).
_REPRESENTATION_FIELDS: Final = (
    "point_in_time_binding",
    "availability_bindings",
    "precedence_bindings",
    "parser_bindings",
)


class AuthorityRefused(DegradationOperationRefused):
    """An authority could not answer, or answered against a rule; ``code`` names which."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_ready(value: Any) -> Any:
    """A plain JSON copy (canonical JSON round trip)."""
    return json.loads(canonical_json(value))


# ======================================================================================
# 3. the closed metric registry
# ======================================================================================


@dataclass(frozen=True, slots=True)
class MonitoringMetricDefinition:
    """One monitorable metric: its name, versioned definition id, the baseline gate it
    reproduces (id + the gate's exact ``metric`` label) and the validation function it calls."""

    metric_name: str
    definition_id: str
    version: str
    baseline_gate_id: str
    baseline_gate_metric: str
    implementation: str
    compute: Callable[[ValidationProfile, PeriodReturns], Decimal | None] = field(
        repr=False, compare=False
    )

    @property
    def definition_ref(self) -> str:
        return f"{self.definition_id}@{self.version}"

    def payload(self) -> dict[str, str]:
        return {
            "metric": self.metric_name,
            "definition": self.definition_ref,
            "baseline_gate_id": self.baseline_gate_id,
            "baseline_gate_metric": self.baseline_gate_metric,
            "implementation": self.implementation,
            "value_representation": GATE_VALUE_QUANTIZATION,
        }


_BREAKEVEN_GATE: Final = "G4.cost_stress.breakeven"
_BREAKEVEN_METRIC: Final = "breakeven_cost_multiple[>=]"


def _g4_cost_stress_breakeven(
    profile: ValidationProfile, returns: PeriodReturns
) -> Decimal | None:
    """``G4.cost_stress.breakeven`` of ``cost_stress_check`` on ``returns`` — the function that
    produced the baseline gate, unchanged. Its insufficient-evidence gate (another metric label,
    ``no_cost_no_trades``) is *missing*; a gate without an exact value is refused (a float-only
    value is never used, as ADR-0067 refuses a float-only baseline)."""
    check = cost_stress_check(profile, returns)
    gates = [gate for gate in check.gates if gate.gate_id == _BREAKEVEN_GATE]
    if len(gates) != 1:
        raise AuthorityRefused(
            METRIC_REFUSED, f"cost_stress_check produced {len(gates)} {_BREAKEVEN_GATE} gates"
        )
    gate = gates[0]
    if gate.metric != _BREAKEVEN_METRIC:
        return None
    if gate.value_exact is None:
        raise AuthorityRefused(
            METRIC_REFUSED,
            f"{_BREAKEVEN_GATE} has no exact value (the Profile's threshold is float-only)",
        )
    return gate.value_exact


#: The closed registry (ADR-0098 §3). A Profile metric not listed here is ``metric_undefined``.
MONITORING_METRICS: Final[Mapping[str, MonitoringMetricDefinition]] = MappingProxyType(
    {
        definition.metric_name: definition
        for definition in (
            MonitoringMetricDefinition(
                metric_name="breakeven_cost_multiple",
                definition_id="hlens.p11.metric.breakeven_cost_multiple",
                version="1.0.0",
                baseline_gate_id=_BREAKEVEN_GATE,
                baseline_gate_metric=_BREAKEVEN_METRIC,
                implementation=(
                    "research.validation.robustness.cost_stress_check"
                    f"[{_BREAKEVEN_GATE}] over research.validation.returns.from_backtest"
                ),
                compute=_g4_cost_stress_breakeven,
            ),
        )
    }
)


def _ruled_definitions(profile: ValidationProfile) -> tuple[MonitoringMetricDefinition, ...]:
    try:
        monitor = DegradationMonitor.from_profile(profile)
    except ValueError as exc:
        raise AuthorityRefused(
            METRIC_REFUSED, f"the Profile's degradation thresholds: {exc}"
        ) from exc
    ruled = sorted({rule.metric for rule in monitor.rules})
    undefined = [metric for metric in ruled if metric not in MONITORING_METRICS]
    if undefined:
        raise AuthorityRefused(
            METRIC_UNDEFINED,
            f"the Profile rules {undefined}, which the closed metric registry "
            f"{METRIC_REGISTRY_ID} does not define",
        )
    return tuple(MONITORING_METRICS[metric] for metric in ruled)


def _check_baseline_gates(
    report: ValidationReport,
    baseline: BaselineMetricSet,
    definitions: Sequence[MonitoringMetricDefinition],
) -> None:
    gate_ids = dict(baseline.gate_ids)
    for definition in definitions:
        metric = definition.metric_name
        if gate_ids.get(metric) != definition.baseline_gate_id:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH,
                f"the baseline of {metric!r} cites gate {gate_ids.get(metric)!r}, not the "
                f"definition's {definition.baseline_gate_id!r}",
            )
        gates = [gate for gate in report.gates if gate.gate_id == definition.baseline_gate_id]
        if len(gates) != 1 or gates[0].metric != definition.baseline_gate_metric:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH,
                f"the report has no single {definition.baseline_gate_id!r} gate reporting "
                f"{definition.baseline_gate_metric!r}",
            )
        if gates[0].value_exact is None:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH,
                f"gate {definition.baseline_gate_id!r} has no exact value",
            )


# ======================================================================================
# 2. the source
# ======================================================================================


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """``(dataset_id, manifest_hash)`` plus the manifest's bound snapshot, policy, rule and
    universe versions (a JSON-ready copy of each)."""

    dataset_id: str
    manifest_hash: str
    contract_schema_version: str
    dataset_table: str
    dataset_snapshot_id: str
    data_type: str
    point_in_time_hash: str
    bindings_json: str

    @classmethod
    def of(cls, manifest: ResearchDatasetEvidenceManifest) -> SourceIdentity:
        pit = manifest.point_in_time
        bindings = {
            "universe_spec": manifest.universe_spec.model_dump(mode="json"),
            "rule": manifest.rule.model_dump(mode="json"),
            "snapshot_bindings": dict(sorted(pit.snapshot_bindings.items())),
            "knowledge_cutoff": _utc_text(pit.knowledge_cutoff),
            "simulation_time": None
            if pit.simulation_time is None
            else _utc_text(pit.simulation_time),
            **{
                name: _json_ready(pit.model_dump(mode="json")[name])
                for name in _REPRESENTATION_FIELDS
            },
        }
        return cls(
            dataset_id=manifest.selection_id,
            manifest_hash=manifest.content_hash(),
            contract_schema_version=manifest.schema_version,
            dataset_table=manifest.dataset.table,
            dataset_snapshot_id=manifest.dataset.snapshot_id,
            data_type=manifest.data_type,
            point_in_time_hash=pit.content_hash(),
            bindings_json=canonical_json(bindings),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "manifest_hash": self.manifest_hash,
            "contract_schema_version": self.contract_schema_version,
            "dataset_table": self.dataset_table,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "data_type": self.data_type,
            "point_in_time_hash": self.point_in_time_hash,
            "bindings": json.loads(self.bindings_json),
        }


def _load_manifest(catalog: DatasetCatalog, manifest_hash: str, what: str) -> AnyDatasetManifest:
    try:
        manifest = load_any_manifest(catalog.builder, manifest_hash, catalog.evidence_verifier)
    except (DatasetBindingError, CatalogIntegrityError, ManifestFormError) as exc:
        raise AuthorityRefused(
            SOURCE_UNAVAILABLE, f"the {what} manifest does not load and verify: {exc}"
        ) from exc
    if manifest.content_hash() != manifest_hash:
        raise AuthorityRefused(SOURCE_UNAVAILABLE, f"the {what} manifest is not {manifest_hash}")
    return manifest


def _persisted_hashes(catalog: DatasetCatalog, dataset_id: str) -> set[str]:
    """Every v3 manifest content hash persisted under ``dataset_id`` (``selection_id``)."""
    try:
        rows = catalog.adapter.scan_columns(
            DATASET_EVIDENCE_MANIFESTS.table,
            columns=("manifest_content_hash",),
            row_filter=EqualTo("selection_id", dataset_id),  # type: ignore[call-arg, arg-type]
        )
    except TableNotFound:
        return set()
    return {str(value) for value in rows.column("manifest_content_hash").to_pylist()}


def _scope_mismatch(
    source: ResearchDatasetEvidenceManifest, baseline: AnyDatasetManifest
) -> str | None:
    if not isinstance(baseline, ResearchDatasetEvidenceManifest):
        return (
            "the baseline dataset is a v2 manifest, which records no bar specification "
            "(data_type); equality cannot be proven"
        )
    if source.universe_spec != baseline.universe_spec:
        return "the universe (universe_spec) differs"
    if source.data_type != baseline.data_type:
        return f"the bar specification differs ({source.data_type} vs {baseline.data_type})"
    if source.dataset.table != baseline.dataset.table:
        return "the representation (dataset table) differs"
    for name in _REPRESENTATION_FIELDS:
        if getattr(source.point_in_time, name) != getattr(baseline.point_in_time, name):
            return f"the representation ({name}) differs"
    return None


def _resolve_source(
    catalog: DatasetCatalog,
    *,
    dataset_id: str,
    manifest_hash: str,
    baseline_manifest: AnyDatasetManifest,
    window: ObservationWindow,
) -> ResearchDatasetEvidenceManifest:
    persisted = _persisted_hashes(catalog, dataset_id)
    if len(persisted) > 1:
        raise AuthorityRefused(
            SOURCE_CONFLICT,
            f"dataset {dataset_id!r} has {len(persisted)} manifests with different content "
            "hashes; none is chosen",
        )
    if not persisted:
        raise AuthorityRefused(SOURCE_UNAVAILABLE, f"dataset {dataset_id!r} has no v3 manifest")
    if manifest_hash not in persisted:
        raise AuthorityRefused(
            SOURCE_IDENTITY_MISMATCH,
            f"manifest {manifest_hash} is not the manifest of dataset {dataset_id!r}",
        )
    manifest = _load_manifest(catalog, manifest_hash, "source")
    if not isinstance(manifest, ResearchDatasetEvidenceManifest):
        raise AuthorityRefused(SOURCE_UNAVAILABLE, "the source manifest is not a v3 manifest")
    if manifest.selection_id != dataset_id:
        raise AuthorityRefused(
            SOURCE_IDENTITY_MISMATCH, f"manifest {manifest_hash} is not of dataset {dataset_id!r}"
        )
    mismatch = _scope_mismatch(manifest, baseline_manifest)
    if mismatch is not None:
        raise AuthorityRefused(SOURCE_SCOPE_MISMATCH, mismatch)
    data = manifest.dataset
    if data.time_range_start > window.start or data.time_range_end < window.end:
        raise AuthorityRefused(
            SOURCE_INCOMPLETE,
            f"the dataset covers [{_utc_text(data.time_range_start)}, "
            f"{_utc_text(data.time_range_end)}), not the whole window",
        )
    return manifest


def _window_bars(
    catalog: DatasetCatalog,
    manifest_hash: str,
    instruments: tuple[str, ...],
    window: ObservationWindow,
) -> DatasetPriceBars:
    try:
        prices = backtest_bars_from_dataset(
            catalog.adapter,
            catalog.storage,
            builder=catalog.builder,
            manifest_content_hash=manifest_hash,
            symbols=instruments,
            price_cutoff=window.end,
            start=window.start,
            end=window.end,
            manifest_cache=catalog.manifest_cache,
            evidence_verifier=catalog.evidence_verifier,
        )
    except DatasetBarsError as exc:
        raise AuthorityRefused(SOURCE_INCOMPLETE, f"the window's bars: {exc}") from exc
    except (DatasetBindingError, CatalogIntegrityError, ManifestFormError) as exc:
        raise AuthorityRefused(SOURCE_UNAVAILABLE, f"the window's bars: {exc}") from exc
    if prices.manifest_content_hash != manifest_hash:
        raise AuthorityRefused(SOURCE_UNAVAILABLE, "the bars are bound to another manifest")
    if prices.price_cutoff > window.end:
        raise AuthorityRefused(SOURCE_INCOMPLETE, "the bars' price cutoff is after the window end")
    for bar in prices.bars:
        if bar.instrument not in instruments:
            raise AuthorityRefused(SOURCE_UNAVAILABLE, f"an unrequested {bar.instrument} bar")
        if bar.available_time > window.end:
            raise AuthorityRefused(
                SOURCE_INCOMPLETE, f"a {bar.instrument} bar is available after the window end"
            )
    for instrument in instruments:
        mine = sorted(
            (bar for bar in prices.bars if bar.instrument == instrument),
            key=lambda bar: bar.interval_start,
        )
        if not mine:
            raise AuthorityRefused(SOURCE_INCOMPLETE, f"no {instrument} bar in the window")
        if mine[0].interval_start != window.start or mine[-1].interval_end != window.end:
            raise AuthorityRefused(
                SOURCE_INCOMPLETE, f"the {instrument} bars do not span exactly the window"
            )
        for earlier, later in pairwise(mine):
            if later.interval_start != earlier.interval_end:
                raise AuthorityRefused(
                    SOURCE_INCOMPLETE,
                    f"{instrument} misses bars in [{_utc_text(earlier.interval_end)}, "
                    f"{_utc_text(later.interval_start)})",
                )
    return prices


# ======================================================================================
# returns through the BacktestProvider protocol (H7)
# ======================================================================================


@dataclass(frozen=True, slots=True)
class TargetSourceIdentity:
    """What a ``WindowTargetSource`` declares it runs; checked against the baseline run's
    reproducibility tuple (module docs)."""

    strategy_ref: Ref
    strategy_spec_hash: str
    risk_policy_ref: Ref | None
    risk_policy_hash: str | None
    params: Mapping[str, str | int | float | bool]
    plugins: Mapping[str, str]
    instruments: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "strategy_ref": str(self.strategy_ref),
            "strategy_spec_hash": self.strategy_spec_hash,
            "risk_policy_ref": None if self.risk_policy_ref is None else str(self.risk_policy_ref),
            "risk_policy_hash": self.risk_policy_hash,
            "params": _json_ready(dict(self.params)),
            "plugins": dict(sorted(self.plugins.items())),
            "instruments": list(self.instruments),
        }


class WindowTargetSource(Protocol):
    """The admitted strategy's decision pipeline (features → signals → ``StrategyProvider`` →
    ``RiskProvider``) evaluated over the window's proven bars. Caller code: this module binds its
    declared ``identity`` and checks every target it returns, it does not re-derive them."""

    @property
    def identity(self) -> TargetSourceIdentity: ...

    def targets(
        self, bars: tuple[PriceBar, ...], window: ObservationWindow
    ) -> Sequence[TargetPosition]: ...


@dataclass(frozen=True)
class WindowExecution:
    """How the window's return series is produced: provider, bound cost model, equity, targets."""

    backtester: BacktestProvider
    cost_model: CostModelSpec
    initial_equity: Decimal
    targets: WindowTargetSource


def _backtest_costs(cost_model: CostModelSpec) -> BacktestCostModel:
    """Exactly the rates of the bound ``CostModelSpec`` (``research.loop.trials.TrialComponents
    .backtest_costs``, the mapping the baseline run's backtests used)."""
    return BacktestCostModel(
        name=cost_model.name,
        version=cost_model.version,
        fee_rate=cost_model.fee_rate_per_side,
        slippage_rate=cost_model.slippage_rate_per_side,
    )


def _check_execution(
    execution: WindowExecution, run: ExperimentRun
) -> tuple[BacktestProviderDescriptor, TargetSourceIdentity]:
    if not isinstance(execution, WindowExecution):
        raise AuthorityRefused(EXECUTION_MISMATCH, "execution must be a WindowExecution")
    repro = run.repro
    descriptor = getattr(execution.backtester, "descriptor", None)
    if not isinstance(descriptor, BacktestProviderDescriptor):
        raise AuthorityRefused(EXECUTION_MISMATCH, "the backtester has no provider descriptor")
    if repro.plugin_versions.get(descriptor.plugin_key) != descriptor.content_hash():
        raise AuthorityRefused(
            EXECUTION_MISMATCH,
            f"backtester {descriptor.plugin_key} is not the one the baseline run recorded",
        )
    cost = execution.cost_model
    if not isinstance(cost, CostModelSpec):
        raise AuthorityRefused(EXECUTION_MISMATCH, "cost_model must be a CostModelSpec")
    if cost.ref.target_identity() != repro.cost_model_ref.target_identity() or (
        repro.dependency_hashes.get(str(cost.ref)) != cost.content_hash()
    ):
        raise AuthorityRefused(
            EXECUTION_MISMATCH, f"cost model {cost.ref} is not the baseline run's"
        )
    equity = execution.initial_equity
    if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
        raise AuthorityRefused(EXECUTION_MISMATCH, "initial_equity must be a positive Decimal")
    identity = getattr(execution.targets, "identity", None)
    if not isinstance(identity, TargetSourceIdentity):
        raise AuthorityRefused(EXECUTION_MISMATCH, "the target source declares no identity")
    strategy = repro.strategy_ref
    if strategy is None or identity.strategy_ref.target_identity() != strategy.target_identity():
        raise AuthorityRefused(EXECUTION_MISMATCH, "the target source runs another strategy")
    if repro.dependency_hashes.get(str(strategy)) != identity.strategy_spec_hash:
        raise AuthorityRefused(EXECUTION_MISMATCH, "the strategy spec hash is not the baseline's")
    risk = repro.risk_policy_ref
    if (risk is None) != (identity.risk_policy_ref is None) or (
        risk is not None
        and identity.risk_policy_ref is not None
        and risk.target_identity() != identity.risk_policy_ref.target_identity()
    ):
        raise AuthorityRefused(EXECUTION_MISMATCH, "the target source runs another risk policy")
    expected_risk_hash = None if risk is None else repro.dependency_hashes.get(str(risk))
    if identity.risk_policy_hash != expected_risk_hash:
        raise AuthorityRefused(EXECUTION_MISMATCH, "the risk policy hash is not the baseline's")
    if dict(identity.params) != dict(repro.params):
        raise AuthorityRefused(EXECUTION_MISMATCH, "the strategy params are not the baseline's")
    if not identity.plugins:
        raise AuthorityRefused(EXECUTION_MISMATCH, "the target source declares no plugins")
    for key, value in identity.plugins.items():
        if repro.plugin_versions.get(key) != value:
            raise AuthorityRefused(
                EXECUTION_MISMATCH, f"plugin {key} is not the one the baseline run recorded"
            )
    instruments = identity.instruments
    if (
        not instruments
        or len(set(instruments)) != len(instruments)
        or not all(isinstance(item, str) and item for item in instruments)
    ):
        raise AuthorityRefused(EXECUTION_MISMATCH, "the target source's instruments are invalid")
    return descriptor, identity


def _window_returns(
    execution: WindowExecution,
    descriptor: BacktestProviderDescriptor,
    identity: TargetSourceIdentity,
    bars: tuple[PriceBar, ...],
    window: ObservationWindow,
) -> tuple[PeriodReturns, dict[str, str]]:
    try:
        targets = tuple(execution.targets.targets(bars, window))
    except (ValueError, TypeError) as exc:
        raise AuthorityRefused(EXECUTION_MISMATCH, f"the target source refused: {exc}") from exc
    for target in targets:
        if not isinstance(target, TargetPosition):
            raise AuthorityRefused(EXECUTION_MISMATCH, "a target is not a TargetPosition")
        if not window.contains(target.decision_time) or target.instrument not in (
            identity.instruments
        ):
            raise AuthorityRefused(EXECUTION_MISMATCH, "a target is outside the window's scope")
        latest = target.latest_input_available_time
        if latest is not None and latest > window.end:
            raise AuthorityRefused(
                EXECUTION_MISMATCH, "a target used an input available after the window end"
            )
    try:
        request = BacktestRequest(
            cost_model=_backtest_costs(execution.cost_model),
            initial_equity=execution.initial_equity,
            bars=bars,
            targets=targets,
        )
        raw = execution.backtester.run(request)
        if not isinstance(raw, BacktestResult):
            raise TypeError("the backtester did not return a BacktestResult")
        result = BacktestResult.model_validate_json(raw.model_dump_json())
        result.check_answers(request, descriptor)
        returns = from_backtest(result)
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuthorityRefused(EXECUTION_MISMATCH, f"the window backtest: {exc}") from exc
    if any(not (window.start < moment <= window.end) for moment in returns.times):
        raise AuthorityRefused(EXECUTION_MISMATCH, "an equity point lies outside the window")
    return returns, {
        "backtest_provider": descriptor.plugin_key,
        "backtest_provider_hash": descriptor.content_hash(),
        "cost_model_ref": str(execution.cost_model.ref),
        "cost_model_hash": execution.cost_model.content_hash(),
        "initial_equity": str(execution.initial_equity),
        "backtest_request_hash": request.content_hash(),
        "backtest_result_hash": result.result_hash,
    }


# ======================================================================================
# 4. provenance and the resolver
# ======================================================================================

_PROVENANCE_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AuthorityProvenance:
    """Where every authority-resolved input came from (ADR-0098 §4); constructible only by
    ``resolve_degradation_inputs`` (an accidental-misuse boundary, not a signature)."""

    subject: Ref
    lifecycle_head: LifecycleHead
    lifecycle_anchor: str
    lifecycle_history_hash: str
    lifecycle_record_hashes: tuple[str, ...]
    source: SourceIdentity
    baseline_run_id: str
    baseline_manifest_hash: str
    validation_report_hash: str
    profile_ref: str
    profile_hash: str
    metric_definitions: tuple[MonitoringMetricDefinition, ...]
    execution_json: str
    recent_manifest_hash: str
    as_of: datetime
    window_start: datetime
    window_end: datetime

    def __init__(self, *, _token: object, **values: Any) -> None:
        if _token is not _PROVENANCE_TOKEN:
            raise TypeError(
                "AuthorityProvenance can only be created by resolve_degradation_inputs"
            )
        names = {item.name for item in fields(self)}
        if set(values) != names:
            raise TypeError(f"AuthorityProvenance needs exactly the fields {sorted(names)}")
        for name in names:
            object.__setattr__(self, name, values[name])

    def payload(self) -> dict[str, Any]:
        """The JSON-ready ``evidence.authority`` object (hash-bound by the report writer)."""
        return {
            "format": AUTHORITY_FORMAT,
            "subject": str(self.subject),
            "lifecycle": {
                "head": self.lifecycle_head.payload(),
                "anchor": self.lifecycle_anchor,
                "history_hash": self.lifecycle_history_hash,
                "record_hashes": list(self.lifecycle_record_hashes),
            },
            "source": self.source.payload(),
            "baseline": {
                "run_id": self.baseline_run_id,
                "manifest_hash": self.baseline_manifest_hash,
                "validation_report_hash": self.validation_report_hash,
                "profile_ref": self.profile_ref,
                "profile_hash": self.profile_hash,
            },
            "metrics": {
                "registry": METRIC_REGISTRY_ID,
                "definitions": [definition.payload() for definition in self.metric_definitions],
            },
            "execution": json.loads(self.execution_json),
            "recent_manifest_hash": self.recent_manifest_hash,
            "as_of": _utc_text(self.as_of),
            "window_start": _utc_text(self.window_start),
            "window_end": _utc_text(self.window_end),
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True, slots=True)
class AuthorityResolution:
    """The resolver's outputs: ``run_degradation_check``'s ``lifecycle`` and ``recent`` inputs
    and the provenance to pass as its ``authority``."""

    lifecycle: LifecycleHistory
    recent: RecentMetricSet
    provenance: AuthorityProvenance


@dataclass(frozen=True)
class AuthorityEnvironment:
    """What a command line cannot construct on its own (``degradation_cli``): the catalog, the
    window execution and the baseline run / dataset. Supplied only by an embedding caller."""

    catalog: DatasetCatalog
    execution: WindowExecution
    baseline_run: ExperimentRun
    baseline_manifest_hash: str


def _resolve_lifecycle(
    lifecycle: LifecycleRegistry, head: LifecycleHead, subject: Ref
) -> tuple[LifecycleHead, LifecycleHistory, tuple[str, ...]]:
    if not isinstance(lifecycle, LifecycleRegistry):
        raise AuthorityRefused(LIFECYCLE_UNAVAILABLE, "lifecycle must be an open LifecycleRegistry")
    try:
        pinned = lifecycle.verify_head(head)
        active = lifecycle.active_set(pinned)
        replayed = lifecycle.lifecycle_of(subject, pinned)
    except UnknownHead as exc:
        raise AuthorityRefused(LIFECYCLE_HEAD_UNKNOWN, str(exc)) from exc
    except RegistryError as exc:
        raise AuthorityRefused(LIFECYCLE_UNAVAILABLE, str(exc)) from exc
    if not active.contains(subject) or replayed.current_state is not LifecycleState.ACTIVE:
        raise AuthorityRefused(
            LIFECYCLE_NOT_ACTIVE,
            f"{subject} replays to {replayed.current_state} at head "
            f"{pinned.record_count}/{pinned.last_record_hash}",
        )
    return pinned, replayed.history, replayed.record_hashes


def _check_baseline_run(
    subject: Ref,
    profile: ValidationProfile,
    report: ValidationReport,
    run: ExperimentRun,
) -> None:
    if not isinstance(run, ExperimentRun):
        raise AuthorityRefused(BASELINE_BINDING_MISMATCH, "baseline_run must be an ExperimentRun")
    if report.subject.target_identity() != subject.target_identity():
        raise AuthorityRefused(BASELINE_BINDING_MISMATCH, "the report is of another subject")
    if report.run_id != run.run_id or report.experiment_hash != run.experiment_hash:
        raise AuthorityRefused(BASELINE_BINDING_MISMATCH, "the report does not cite this run")
    profile_hash = profile.content_hash()
    if (str(report.validation_profile), report.validation_profile_hash) != (
        str(profile.ref),
        profile_hash,
    ) or (str(run.repro.validation_profile), run.repro.validation_profile_hash) != (
        str(profile.ref),
        profile_hash,
    ):
        raise AuthorityRefused(
            BASELINE_BINDING_MISMATCH, "the report or run binds another Profile"
        )


def resolve_degradation_inputs(
    *,
    subject: Ref,
    lifecycle: LifecycleRegistry,
    head: LifecycleHead,
    profile: ValidationProfile,
    baseline_report: ValidationReport,
    baseline: BaselineMetricSet,
    baseline_run: ExperimentRun,
    baseline_manifest_hash: str,
    catalog: DatasetCatalog,
    dataset_id: str,
    manifest_hash: str,
    execution: WindowExecution,
    window: ObservationWindow,
) -> AuthorityResolution:
    """Resolve ``run_degradation_check``'s lifecycle and recent inputs from the ADR-0098
    authorities (module docs). Raises ``AuthorityRefused`` (with a ``code``) on any failed rule;
    reads only, writes nothing, reads no clock."""
    if not isinstance(subject, Ref):
        raise DegradationOperationRefused("subject must be a Ref")
    if not isinstance(window, ObservationWindow):
        raise DegradationOperationRefused("window must be an ObservationWindow")
    if not isinstance(baseline, BaselineMetricSet):
        raise DegradationOperationRefused("baseline must be a BaselineMetricSet")
    if not isinstance(profile, ValidationProfile) or not isinstance(
        baseline_report, ValidationReport
    ):
        raise DegradationOperationRefused("profile and baseline_report must be their contracts")
    if not isinstance(catalog, DatasetCatalog) or catalog.evidence_verifier is None:
        raise AuthorityRefused(
            SOURCE_UNAVAILABLE, "a DatasetCatalog with its v3 evidence verifier is required"
        )
    if not isinstance(dataset_id, str) or _SELECTION_ID.fullmatch(dataset_id) is None:
        raise AuthorityRefused(SOURCE_IDENTITY_MISMATCH, "dataset_id is not a v3 selection id")
    for label, value in (
        ("manifest_hash", manifest_hash),
        ("baseline_manifest_hash", baseline_manifest_hash),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise AuthorityRefused(SOURCE_IDENTITY_MISMATCH, f"{label} is not a SHA-256 hash")

    # 1. lifecycle at the explicit head
    pinned, history, record_hashes = _resolve_lifecycle(lifecycle, head, subject)
    anchor = ANCHOR_PRESENT if lifecycle.anchored else ANCHOR_ABSENT

    # 3a. every ruled metric is defined, and the baseline cites each definition's gate
    definitions = _ruled_definitions(profile)
    _check_baseline_gates(baseline_report, baseline, definitions)

    # the baseline run, its dataset and the execution identities
    _check_baseline_run(subject, profile, baseline_report, baseline_run)
    baseline_manifest = _load_manifest(catalog, baseline_manifest_hash, "baseline")
    if baseline_manifest.dataset not in baseline_run.repro.dataset_snapshots:
        raise AuthorityRefused(
            SOURCE_SCOPE_MISMATCH,
            "the baseline manifest's dataset is not one the baseline run read",
        )
    descriptor, identity = _check_execution(execution, baseline_run)

    # 2. the source and the window's proven bars
    manifest = _resolve_source(
        catalog,
        dataset_id=dataset_id,
        manifest_hash=manifest_hash,
        baseline_manifest=baseline_manifest,
        window=window,
    )
    prices = _window_bars(catalog, manifest_hash, identity.instruments, window)

    # returns through the provider, then 3b. each definition's validation function
    returns, execution_payload = _window_returns(
        execution, descriptor, identity, prices.bars, window
    )
    values: dict[str, Decimal] = {}
    for definition in definitions:
        try:
            value = definition.compute(profile, returns)
        except AuthorityRefused:
            raise
        except ValueError as exc:
            raise AuthorityRefused(
                METRIC_REFUSED, f"{definition.definition_ref}: {exc}"
            ) from exc
        if value is not None:
            values[definition.metric_name] = value

    source = SourceIdentity.of(manifest)
    last_bar = max(prices.bars, key=lambda bar: (bar.interval_start, bar.instrument))
    observed = max(bar.available_time for bar in prices.bars)
    set_id = "authority:" + content_hash(
        {
            "dataset_id": dataset_id,
            "manifest_hash": manifest_hash,
            "lifecycle_head": pinned.payload(),
            "window": window.payload(),
        }
    )
    recent_manifest = RecentMetricManifest(
        subject=subject,
        profile_ref=profile.ref,
        profile_hash=profile.content_hash(),
        window=window,
        observation_set_id=set_id,
        method_id=METRIC_REGISTRY_ID,
        sources=(
            ObservationSource(
                source_id=f"v3:{manifest.dataset.table}",
                source_hash=manifest_hash,
                event_time=last_bar.interval_start,
                observed_time=observed,
            ),
        ),
        metrics=values,
    )
    recent = RecentMetricSet(
        manifest=recent_manifest, manifest_hash=recent_manifest.content_hash()
    )
    provenance = AuthorityProvenance(
        _token=_PROVENANCE_TOKEN,
        subject=subject,
        lifecycle_head=pinned,
        lifecycle_anchor=anchor,
        lifecycle_history_hash=history.content_hash(),
        lifecycle_record_hashes=record_hashes,
        source=source,
        baseline_run_id=baseline_run.run_id,
        baseline_manifest_hash=baseline_manifest_hash,
        validation_report_hash=baseline_report.content_hash(),
        profile_ref=str(profile.ref),
        profile_hash=profile.content_hash(),
        metric_definitions=definitions,
        execution_json=canonical_json(
            {"target_source": identity.payload(), **execution_payload}
        ),
        recent_manifest_hash=recent.manifest_hash,
        as_of=window.end,
        window_start=window.start,
        window_end=window.end,
    )
    return AuthorityResolution(lifecycle=history, recent=recent, provenance=provenance)
