"""Authoritative inputs for the P11 degradation check (ADR-0098; supersedes ADR-0080's block).

ADR-0067's ``run_degradation_check`` takes a caller-declared ``LifecycleHistory`` and a
caller-declared ``RecentMetricManifest`` and says so in its evidence. This module is the ADR-0098
§4 resolver: ``resolve_degradation_inputs`` derives both from three authorities and records where
they came from in an ``AuthorityProvenance`` that ``run_degradation_check(..., authority=...)``
writes into the report's ``evidence.authority``. The explicit caller-declared path is unchanged and
carries no ``authority`` field — that absence is how the two evidence strengths stay apart.

**Evaluation time (ADR-0098 修订 2 §1).** Every resolution takes an explicit ``as_of`` (UTC,
``as_of >= window.end``): the window's last bar is only available after the window ends
(``available_time = max(interval_end, raw.available_time + latency)``), so the evaluation moment is
separate from the window. Nothing reads a clock; ``as_of`` is recorded in the provenance and in the
recent manifest.

**1. Lifecycle (ADR-0098 §1, 修订 2 §2 / §6).**
``infrastructure.registry.lifecycle.LifecycleRegistry`` opened as a **read-only snapshot**
(``LifecycleRegistry.open_snapshot``: no writer lock, no anchor crash-recovery write; a writer
instance is ``lifecycle_unavailable``) at an **explicit** head (the latest ``registry.head`` or
one the caller pinned; a head not on the chain is ``lifecycle_head_unknown``). The subject's
replay at that head is **truncated** to the transitions with ``occurred_at <= as_of``
(``occurred_at`` is non-decreasing, so this is a prefix). The truncated replay must end in
ACTIVE, the transition into that ACTIVE state must have ``occurred_at <= window.start``, and no
transition may have ``occurred_at`` in ``(window.start, as_of]`` — else ``lifecycle_not_active``
(together these mean the subject entered ACTIVE strictly before the window and stayed there
through ``as_of``). The truncated history is the ``LifecycleHistory`` handed to the operation
(its hash is what ``run_degradation_check`` binds), and the head identity plus the truncated
record hashes go into the provenance, with ``anchor`` = ``present`` when the snapshot was
verified against its external anchor (any mismatch is refused on open) or ``absent`` (not
detectable; recorded, not refused — ADR-0098 修订 1).

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
- instruments (修订 2 §3): the decision pipeline's declared ``TargetSourceIdentity.instruments``
  must equal the baseline manifest's universe members **and** the source manifest's universe
  members, as sets of Canonical symbols → ``source_scope_mismatch`` otherwise. A manifest's members
  are its v3 ``members`` evidence stream (root-hash authenticated, one record at a time); a member
  is a Canonical symbol only through a ``DegradedEpisodeKey`` of that instrument's venue, type and
  symbol (the rule ``research.loop.dataset_source`` uses; an episode with a stable product id names
  no symbol, so it is refused rather than guessed);
- only ``available_time <= as_of`` (``backtest_bars_from_dataset(price_cutoff=as_of)`` refuses a
  later bar instead of dropping it; ``as_of`` must not be after the manifest's point-in-time view)
  over the bars whose ``interval_start`` is in ``[window.start, window.end)``, of which only the
  bars with ``interval_end <= window.end`` are kept; per instrument the kept bars must tile
  ``[window.start, window.end)`` exactly (first starts at ``window.start``, each starts where the
  previous ended, the last ends at ``window.end``). A missing bar, a late bar or a window the
  manifest does not cover → ``source_incomplete``; nothing is filled or interpolated. The decision
  pipeline still sees each bar only from its own ``available_time`` (no look-ahead is introduced).

**3. Metrics (ADR-0098 §3; ADR-0100 item 3).** ``MONITORING_METRIC_DEFINITIONS`` is the closed
registry (``MONITORING_METRICS``: by metric name; ``METRIC_REGISTRY_ID`` 2.0.0). Several gates
report the same metric label (``breakeven_cost_multiple`` is G2, G4 and G5), so a definition is
selected by the metric **and** the gate its baseline cites: a ``MonitoringMetricDefinition`` names
that gate (an id, or a template with ``<i>`` = list index / ``<seed>`` = control seed, optionally
with the per-instrument suffix ``.instrument.<name>``), the gate's exact ``metric`` label, **the
validation function that produced the gate**, where the window value comes from (``evidence``)
and over which data (``window_scope``, recorded in the provenance with ``definition_id@version``
and the concrete gate id). Nothing is re-implemented and no formula is invented: every value is
the gate the same function writes with the same Profile parameters on the window's data. When
the function writes its own insufficient-evidence variant of the gate (another label, e.g.
``no_cost_no_trades``, ``too_few_trades``) or its stage did not run (the validator stops after a
failed stage), the metric is **missing**, as in validation; a gate without an exact value is
``metric_refused`` (a float-only value is never used).

- ``window_returns`` (the window's one backtest, ``from_backtest``; no extra input):
  ``breakeven_cost_multiple`` @ ``G4.cost_stress.breakeven`` and
  ``breakeven_cost_multiple_vs_stress`` @ ``G4.cost_stress.<i>`` (``cost_stress_check``; scope
  ``observation_window``); ``positive_window_fraction`` @ ``G4.walk_forward.positive_fraction`` and
  ``max_window_pnl_share`` @ ``G4.walk_forward.max_window_share`` (``walk_forward_check``; scope:
  the Profile's fixed non-overlapping walk-forward test windows over the window's returns).
- ``window_validation``: the baseline validator
  (``research.strategies.validation.PipelineBacktestValidator``) re-run on the window's proven
  bars with the baseline ``ValidatorSetup``'s own inputs (``WindowValidationBinding``: spec,
  metadata incl. ``family_trial_count``, Outcome label spec + provider = the run's recorded
  ``outcome_ref`` / plugin, validation seed ∈ the run's ``seeds``, ``RobustnessParams``, control
  seeds, market benchmark, declared execution model, C-R3 scope = exactly the pipeline's
  instruments; each checked against the run and the baseline report's own gates —
  ``baseline_binding_mismatch`` / ``execution_mismatch``). A baseline run with the ADR-0100
  修订 2 ``hlens.p11.inputs@1.0.0`` record (``research.experiments.run_inputs``: in its hashed
  ``repro.params``) records ``family_trial_count``, the validation seed, the control seeds, the
  ``cscv_partitions`` / ``impact_coefficient`` it used and its state labeller identity; each must
  equal the binding's exactly (canonical JSON; else ``baseline_binding_mismatch``), and then
  every dependency below is proven. A run **without** the record (every run from before the
  amendment; never backfilled or inferred — H3 / H6) does **not** record them (nor does its
  report), so they cannot be proven equal and every ruled definition whose gate depends on one is
  refused (``baseline_input_unrecorded``): ``G3.*``, ``G4.overfitting``,
  ``G2.null_model_percentile``, the single-seed G1 controls and every ``G2.*`` gate after them,
  ``G4.overfitting`` with ``param:cscv_partitions``, ``G4.capacity.*`` with
  ``param:capacity.impact_coefficient``, ``G4.state.*`` with a state labeller.
  Outcome labels are computed by that provider over the window's proven bars of the pinned source
  manifest for the pipeline's own non-flat targets — the evaluation target, never an input
  (C-L2, the validator's own guard). Its trial runner must reproduce the window backtest
  (``G0.reproducibility``); a failed structural gate — G0 bindings / execution, or the G1
  information-flow gates ``G1.outcome_not_input``, ``G1.label_blind_sides``,
  ``G1.sealed_oos_excluded`` (also per instrument) — refuses every metric of the re-run.
  ``validate`` supplies G0 – G3, ``robustness_diagnostic`` G4. Definitions: G1
  ``shuffle_timing_p_value`` / ``shift_timing_p_value`` (base and per-seed gates) and ``..._min_over_seeds`` (scope: all the
  window's computable labels); G2 ``effective_independent_trades``, ``breakeven_cost_multiple``,
  ``breakeven_cost_multiple_vs_stress`` @ ``G2.cost_stress.<i>``,
  ``percentile_vs_random_entry_null`` and G3 ``net_mean_hac_p_greater_adjusted`` (scope: the
  Profile's walk-forward test folds restricted to the window's labels); G4
  ``probability_of_backtest_overfitting`` / ``one_minus_deflated_sharpe_ratio``,
  ``neighbor_mean_sharpe_over_chosen_sharpe``, ``positive_neighbor_fraction`` (scope: the declared
  parameter family re-run over the window), ``shifted_sharpe_over_base_sharpe`` @
  ``G4.time_alignment.<i>``, ``delayed_breakeven_cost_multiple``, ``positive_instrument_fraction``
  / ``positive_subuniverse_fraction`` (scope: the Profile's variation re-run over the window),
  ``undersampled_state_pnl_share``, ``capacity_notional`` (scope: the window).
- Fixed Profile windows: a definition over the Profile's walk-forward folds / windows
  (``requires_research_window``) needs the observation window inside the Profile's research
  window ``[research_window_start, sealed_oos_boundary)``; otherwise it would need data outside
  the window and is ``metric_undefined`` (the windows are never re-anchored).
- Always ``metric_undefined`` (``REFUSED_METRIC_GATES``): every ``G5.*`` gate (the family's
  one-shot sealed evaluation cannot be re-run on another window) and the reported-only
  ``G2.market_benchmark*`` / ``G2.inverse_control`` / ``G2.cost_report.*`` items (no exact value);
  any other (metric, gate) pair not in the registry as well. A ``window_validation`` definition
  without a binding is ``metric_inputs_unavailable``.

**Returns (H7).** The window's series is one backtest through the ``BacktestProvider`` protocol:
the caller supplies a ``WindowExecution`` — the provider, the bound ``CostModelSpec``, the initial
equity and a ``WindowTargetSource`` (the admitted strategy's decision pipeline over the window's
proven bars). Its declared identities must equal the baseline run's reproducibility tuple
(backtest provider descriptor hash in ``plugin_versions``; cost model ref + content hash in
``dependency_hashes``; strategy ref + spec hash, risk policy ref + hash, params and every declared
plugin hash; params compared as canonical JSON, so ``True``, ``1`` and ``1.0`` differ — 修订 2 §4;
the baseline's params are its strategy params, without the run inputs record) — else
``execution_mismatch``. The target source also declares its decision grid (step, warm-up) and
initial equity (in its identity payload, hence the provenance); they must equal the baseline run's
recorded values — the ``execution`` block of its ``hlens.p11.inputs@1.0.0`` record (ADR-0100
修订 2), compared as canonical JSON (``execution_mismatch`` otherwise). A baseline run without a
valid record (every run from before the amendment; never backfilled or inferred) is refused with
``execution_unrecorded``: an environment value is a caller declaration, not the baseline's. The
record is copied into the provenance (``execution.baseline_run_inputs``). The provider's result is
re-validated and checked with ``BacktestResult.check_answers``; one window yields one sample.

**Baseline binding (修订 2 §5).** The provenance records the ``BaselineMetricSet`` content hash;
``run_degradation_check`` refuses a baseline set whose hash differs.

**Boundaries.** Nothing here reads a clock (the as-of time is the caller's explicit ``as_of``),
schedules, loops, writes the lifecycle, or writes a report; any refusal raises
``AuthorityRefused`` (a ``DegradationOperationRefused`` with a ``code``) before the operation
runs, so no report is written. The ADR-0074 operator and the API do not import this module
(ADR-0098 §4).

**Honest boundary.** The decision pipeline (``WindowTargetSource``) is caller code bound by its
declared identities, not re-derived here; the Lifecycle Registry's actors are declared names. The
command line cannot construct the catalog or the decision pipeline itself, so
``degradation_cli`` refuses the authority mode unless a deployment-trusted
``--authority-environment MODULE:CALLABLE`` factory or an embedding caller supplies an
``AuthorityEnvironment`` (ADR-0098 修订 1; see that module). The default factory is
``research.operations.authority_environment:default_environment`` (ADR-0100 item 4).

Code completion (2026-09-30, CODE_COMPLETE / DEBUG_PENDING; not run, not tested).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import ValidationError
from pyiceberg.expressions import EqualTo

from apps.worker.degradation import DegradationMonitor
from core.contracts.catalog import TableNotFound
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeProvider, OutcomeUsedAsInput
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderDescriptor,
    BacktestProviderError,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    RiskProviderError,
    StrategyProviderError,
    TargetPosition,
)
from core.contracts.universe import (
    DATASET_SELECTION_ID_PATTERN,
    DegradedEpisodeKey,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    UniverseMember,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref, canonical_json, content_hash
from core.domain.research import ExperimentRun, GateResult, ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from infrastructure.bars.dataset import (
    DatasetBarsError,
    DatasetPriceBars,
    backtest_bars_from_dataset,
)
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_EVIDENCE_MANIFESTS
from infrastructure.dataset.evidence import EvidenceError
from infrastructure.dataset.manifests import ManifestFormError
from infrastructure.feature.dataset import (
    AnyDatasetManifest,
    DatasetBindingError,
    iter_manifest_evidence,
    load_any_manifest,
)
from infrastructure.registry.lifecycle import LifecycleHead, LifecycleRegistry, UnknownHead
from infrastructure.registry.registry import RegistryError
from plugins.backtest import BarBacktester, ExecutionModel
from research.experiments.run_inputs import (
    RUN_INPUTS_KEY,
    RunInputs,
    RunInputsError,
    execution_payload,
    recorded_run_inputs,
    strategy_params,
)
from research.loop.dataset_source import DatasetCatalog
from research.operations.degradation import (
    BaselineMetricSet,
    DegradationOperationRefused,
    ObservationSource,
    ObservationWindow,
    RecentMetricManifest,
    RecentMetricSet,
)
from research.strategies.validation import (
    PipelineBacktestValidator,
    TrialRunner,
    ValidatorSetup,
)
from research.validation.g4 import RobustnessParams
from research.validation.gates import (
    GATE_VALUE_QUANTIZATION,
    PARAM_SOURCE_PREFIX,
    PROFILE_FIELD_MISSING,
    profile_has,
)
from research.validation.pipeline import ValidationContext
from research.validation.returns import PeriodReturns, from_backtest
from research.validation.robustness import RobustnessCheck, cost_stress_check, walk_forward_check
from research.validation.splits import midnight_utc

__all__ = [
    "ANCHOR_ABSENT",
    "ANCHOR_PRESENT",
    "AUTHORITY_FORMAT",
    "BASELINE_BINDING_MISMATCH",
    "BASELINE_INPUT_UNRECORDED",
    "EVIDENCE_WINDOW_RETURNS",
    "EXECUTION_UNRECORDED",
    "EVIDENCE_WINDOW_VALIDATION",
    "EXECUTION_MISMATCH",
    "LIFECYCLE_HEAD_UNKNOWN",
    "LIFECYCLE_NOT_ACTIVE",
    "LIFECYCLE_UNAVAILABLE",
    "METRIC_INPUTS_UNAVAILABLE",
    "METRIC_REFUSED",
    "METRIC_REGISTRY_ID",
    "METRIC_UNDEFINED",
    "MONITORING_METRICS",
    "MONITORING_METRIC_DEFINITIONS",
    "REFUSED_METRIC_GATES",
    "SCOPE_PARAM_FAMILY",
    "SCOPE_RERUN",
    "SCOPE_WALK_FORWARD_FOLDS",
    "SCOPE_WALK_FORWARD_WINDOWS",
    "SCOPE_WINDOW",
    "SCOPE_WINDOW_LABELS",
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
    "RuledMetric",
    "SourceIdentity",
    "TargetSourceIdentity",
    "WindowExecution",
    "WindowTargetSource",
    "WindowValidationBinding",
    "resolve_degradation_inputs",
    "universe_symbols",
]

#: Identity of the provenance payload (part of the report's hash-bound evidence).
#: 1.2.0 (ADR-0100 修订 2): ``execution.baseline_run_inputs`` (the baseline run's
#: ``hlens.p11.inputs@1.0.0`` record) and ``window_validation.state_labels_identity``.
AUTHORITY_FORMAT: Final = "hlens.p11.authority-provenance@1.2.0"
#: ``authority.lifecycle.anchor``: the Lifecycle Registry was verified against an external anchor
#: (``present``) or opened without one (``absent``: a rollback of whole trailing records cannot be
#: detected; ADR-0098 修订 1 — recorded, not refused).
ANCHOR_PRESENT: Final = "present"
ANCHOR_ABSENT: Final = "absent"
#: Identity and version of the closed metric registry below (the recent manifest's ``method_id``).
METRIC_REGISTRY_ID: Final = "hlens.p11.monitoring-metrics@2.0.0"

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
#: a ruled ``window_validation`` definition depends on a baseline validator input that neither the
#: baseline report nor its run records (a run without the ``hlens.p11.inputs@1.0.0`` record of
#: ADR-0100 修订 2, or with an invalid one), so the binding's value cannot be proven to be the
#: baseline's (``family_trial_count``, the validation ``seed``, a ``param:`` ``cscv_partitions`` /
#: ``impact_coefficient``, a caller-supplied state labeller): the definition is refused
BASELINE_INPUT_UNRECORDED: Final = "baseline_input_unrecorded"
EXECUTION_MISMATCH: Final = "execution_mismatch"
#: the window run's decision grid (step, warm-up) or initial equity cannot be verified against the
#: baseline run, which does not record them (no valid ``hlens.p11.inputs@1.0.0`` record in its
#: ``repro.params``: a run from before ADR-0100 修订 2 — never backfilled or inferred)
EXECUTION_UNRECORDED: Final = "execution_unrecorded"
METRIC_REFUSED: Final = "metric_refused"
#: a defined metric whose inputs (the ``WindowValidationBinding``) were not supplied
METRIC_INPUTS_UNAVAILABLE: Final = "metric_inputs_unavailable"

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SELECTION_ID: Final = re.compile(DATASET_SELECTION_ID_PATTERN)
#: Canonical symbol → its instrument (venue, type): how a universe member names a symbol.
_CANONICAL_INSTRUMENTS: Final = {item.symbol: item for item in rules.SYMBOLS.values()}
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

#: ``MonitoringMetricDefinition.evidence``: the value comes from the window's one backtest through
#: the ``WindowExecution`` (``from_backtest`` of its result) ...
EVIDENCE_WINDOW_RETURNS: Final = "window_returns"
#: ... or from the baseline validator (``research.strategies.validation.PipelineBacktestValidator``)
#: re-run on the window with the baseline setup's own inputs (``WindowValidationBinding``).
EVIDENCE_WINDOW_VALIDATION: Final = "window_validation"

# ---- window scopes (recorded with every definition; module docs, **3. Metrics**) ---------------
#: The window's own series / labels, nothing outside ``[window.start, window.end)``.
SCOPE_WINDOW: Final = "observation_window"
#: The window's labels, every computable one (the G1 negative controls use all labels).
SCOPE_WINDOW_LABELS: Final = "observation_window:all_computable_labels"
#: The Profile's fixed walk-forward test folds (``splits.walk_forward_folds``), restricted to the
#: window's labels; the window must lie inside the Profile's research window.
SCOPE_WALK_FORWARD_FOLDS: Final = "profile_walk_forward_test_folds:within_observation_window"
#: The Profile's fixed non-overlapping walk-forward test windows (``splits.walk_forward_windows``)
#: over the window's returns; the window must lie inside the Profile's research window.
SCOPE_WALK_FORWARD_WINDOWS: Final = "profile_walk_forward_test_windows:within_observation_window"
#: Every point of the declared ``param_search_space`` re-run over the window (the family).
SCOPE_PARAM_FAMILY: Final = "declared_param_search_space_rerun:within_observation_window"
#: The chosen point re-run over the window with the Profile's variation (delay / offset / scope).
SCOPE_RERUN: Final = "profile_variation_rerun:within_observation_window"

_BREAKEVEN_GATE: Final = "G4.cost_stress.breakeven"
_BREAKEVEN_METRIC: Final = "breakeven_cost_multiple[>=]"
_INDEX: Final = "(?:0|[1-9][0-9]*)"
_SEED: Final = "-?(?:0|[1-9][0-9]*)"
#: ``research.validation.instruments.INSTRUMENT_INFIX`` + an instrument name (no ``.`` / ``|``).
_INSTRUMENT_SUFFIX: Final = r"(?:\.instrument\.[A-Za-z0-9_-]+)?"


def _gate_regex(template: str, per_instrument: bool) -> re.Pattern[str]:
    """``<i>`` = a list index, ``<seed>`` = an int seed; optional per-instrument suffix."""
    body = re.escape(template).replace("<i>", _INDEX).replace("<seed>", _SEED)
    return re.compile("^" + body + (_INSTRUMENT_SUFFIX if per_instrument else "") + "$")


class _WindowInputs:
    """What a definition computes from: the Profile, the window's return series and, lazily, the
    window re-run of the baseline validator (``None`` when no binding was supplied)."""

    __slots__ = ("profile", "returns", "_validation", "_gates")

    def __init__(
        self,
        profile: ValidationProfile,
        returns: PeriodReturns,
        validation: Callable[[], Mapping[str, GateResult]] | None,
    ) -> None:
        self.profile = profile
        self.returns = returns
        self._validation = validation
        self._gates: Mapping[str, GateResult] | None = None

    def validation_gates(self) -> Mapping[str, GateResult]:
        if self._validation is None:
            raise AuthorityRefused(
                METRIC_INPUTS_UNAVAILABLE, "no WindowValidationBinding was supplied"
            )
        if self._gates is None:
            self._gates = self._validation()
        return self._gates


@dataclass(frozen=True, slots=True)
class MonitoringMetricDefinition:
    """One monitorable metric: its name, versioned definition id, the baseline gate(s) it
    reproduces (a gate id or a template with ``<i>`` / ``<seed>``, optionally per instrument, and
    the gate's exact ``metric`` label), the validation function it calls, where its window value
    comes from (``evidence``) and over which data (``window_scope``)."""

    metric_name: str
    definition_id: str
    version: str
    baseline_gate_id: str
    baseline_gate_metric: str
    implementation: str
    window_scope: str
    evidence: str
    compute: Callable[[_WindowInputs, str], Decimal | None] = field(repr=False, compare=False)
    per_instrument: bool = False
    #: the window must lie inside the Profile's research window (the Profile's fixed
    #: walk-forward folds / windows live there); else ``metric_undefined``
    requires_research_window: bool = False

    @property
    def definition_ref(self) -> str:
        return f"{self.definition_id}@{self.version}"

    def matches(self, gate_id: str) -> bool:
        pattern = _gate_regex(self.baseline_gate_id, self.per_instrument)
        return pattern.fullmatch(gate_id) is not None

    def payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric_name,
            "definition": self.definition_ref,
            "baseline_gate_id": self.baseline_gate_id,
            "baseline_gate_metric": self.baseline_gate_metric,
            "per_instrument": self.per_instrument,
            "implementation": self.implementation,
            "evidence": self.evidence,
            "window_scope": self.window_scope,
            "value_representation": GATE_VALUE_QUANTIZATION,
        }


@dataclass(frozen=True, slots=True)
class RuledMetric:
    """A ruled metric bound to its definition and the concrete baseline gate id it reproduces."""

    definition: MonitoringMetricDefinition
    gate_id: str

    @property
    def metric_name(self) -> str:
        return self.definition.metric_name

    def payload(self) -> dict[str, Any]:
        return {**self.definition.payload(), "gate_id": self.gate_id}


def _gate_value(
    gates: Sequence[GateResult] | Mapping[str, GateResult],
    gate_id: str,
    metric_label: str,
    *,
    source: str,
    absent_is_missing: bool,
) -> Decimal | None:
    """The exact value of the one ``gate_id`` gate reporting ``metric_label``. The same gate under
    another label is that function's own insufficient-evidence result: *missing*, as in
    validation. A gate without an exact value is refused (a float-only value is never used, as
    ADR-0067 refuses a float-only baseline)."""
    if isinstance(gates, Mapping):
        found = [gates[gate_id]] if gate_id in gates else []
    else:
        found = [gate for gate in gates if gate.gate_id == gate_id]
    if len(found) > 1 or (not found and not absent_is_missing):
        raise AuthorityRefused(METRIC_REFUSED, f"{source} produced {len(found)} {gate_id} gates")
    if not found:
        return None  # the stage that computes it did not run on the window (validation stops)
    gate = found[0]
    if gate.metric != metric_label:
        return None
    if gate.value_exact is None:
        raise AuthorityRefused(
            METRIC_REFUSED, f"{gate_id} has no exact value (the Profile's threshold is float-only)"
        )
    return gate.value_exact


def _returns_check(
    check: Callable[[ValidationProfile, PeriodReturns], RobustnessCheck], name: str
) -> Callable[[MonitoringMetricDefinition], Callable[[_WindowInputs, str], Decimal | None]]:
    """A ``window_returns`` definition: ``check`` (the G4 function that produced the baseline gate,
    unchanged) on the window's return series; the gate must exist (the function always writes
    it)."""

    def bind(
        definition: MonitoringMetricDefinition,
    ) -> Callable[[_WindowInputs, str], Decimal | None]:
        def compute(inputs: _WindowInputs, gate_id: str) -> Decimal | None:
            result = check(inputs.profile, inputs.returns)
            return _gate_value(
                result.gates,
                gate_id,
                definition.baseline_gate_metric,
                source=name,
                absent_is_missing=False,
            )

        return compute

    return bind


def _validation_gate(
    definition: MonitoringMetricDefinition,
) -> Callable[[_WindowInputs, str], Decimal | None]:
    """A ``window_validation`` definition: the gate of the same id in the window re-run of the
    baseline validator (G0 – G3 of ``validate``, G4 of ``robustness_diagnostic``)."""

    def compute(inputs: _WindowInputs, gate_id: str) -> Decimal | None:
        return _gate_value(
            inputs.validation_gates(),
            gate_id,
            definition.baseline_gate_metric,
            source="the window validation",
            absent_is_missing=True,
        )

    return compute


def _define(
    metric: str,
    gate: str,
    label: str,
    implementation: str,
    scope: str,
    *,
    returns_check: tuple[Callable[[ValidationProfile, PeriodReturns], RobustnessCheck], str]
    | None = None,
    per_instrument: bool = False,
    research_window: bool = False,
    definition_id: str | None = None,
) -> MonitoringMetricDefinition:
    """One registry entry; the definition id defaults to ``hlens.p11.metric.<metric>.<gate>``."""
    slug = re.sub(r"[^a-z0-9_]+", "_", f"{metric}.{gate}".lower()).strip("_")
    evidence = EVIDENCE_WINDOW_VALIDATION if returns_check is None else EVIDENCE_WINDOW_RETURNS
    placeholder = MonitoringMetricDefinition(
        metric_name=metric,
        definition_id=definition_id or f"hlens.p11.metric.{slug}",
        version="1.0.0",
        baseline_gate_id=gate,
        baseline_gate_metric=label,
        implementation=implementation,
        window_scope=scope,
        evidence=evidence,
        compute=lambda inputs, gate_id: None,
        per_instrument=per_instrument,
        requires_research_window=research_window,
    )
    compute = (
        _validation_gate(placeholder)
        if returns_check is None
        else _returns_check(*returns_check)(placeholder)
    )
    return replace(placeholder, compute=compute)


_COST_STRESS: Final = (cost_stress_check, "cost_stress_check")
_WALK_FORWARD: Final = (walk_forward_check, "walk_forward_check")
_RETURNS_SOURCE: Final = " over research.validation.returns.from_backtest of the window backtest"
_VALIDATOR: Final = (
    " via research.strategies.validation.PipelineBacktestValidator re-run on the window "
    "(baseline ValidatorSetup inputs)"
)
_G4_VALIDATOR: Final = (
    " via research.strategies.validation.PipelineBacktestValidator.robustness_diagnostic re-run "
    "on the window (baseline ValidatorSetup inputs)"
)
_IN_SAMPLE: Final = "research.validation.pipeline.run_in_sample"
_ROBUSTNESS: Final = "research.validation.robustness"

#: Every definition of the closed registry (ADR-0098 §3; ADR-0100 item 3). Each calls the
#: validation function that produced its baseline gate, with the same Profile parameters, on the
#: window's data; nothing else is a monitorable metric.
MONITORING_METRIC_DEFINITIONS: Final[tuple[MonitoringMetricDefinition, ...]] = (
    # ---- G4 functions of the window's one return series (no binding needed) -------------------
    _define(
        "breakeven_cost_multiple",
        _BREAKEVEN_GATE,
        _BREAKEVEN_METRIC,
        f"{_ROBUSTNESS}.cost_stress_check[{_BREAKEVEN_GATE}]{_RETURNS_SOURCE}",
        SCOPE_WINDOW,
        returns_check=_COST_STRESS,
        definition_id="hlens.p11.metric.breakeven_cost_multiple",
    ),
    _define(
        "breakeven_cost_multiple_vs_stress",
        "G4.cost_stress.<i>",
        "breakeven_cost_multiple_vs_stress[>=]",
        f"{_ROBUSTNESS}.cost_stress_check[G4.cost_stress.<i>]{_RETURNS_SOURCE}",
        SCOPE_WINDOW,
        returns_check=_COST_STRESS,
    ),
    _define(
        "positive_window_fraction",
        "G4.walk_forward.positive_fraction",
        "positive_window_fraction[>=]",
        f"{_ROBUSTNESS}.walk_forward_check[G4.walk_forward.positive_fraction]{_RETURNS_SOURCE}",
        SCOPE_WALK_FORWARD_WINDOWS,
        returns_check=_WALK_FORWARD,
        research_window=True,
    ),
    _define(
        "max_window_pnl_share",
        "G4.walk_forward.max_window_share",
        "max_window_pnl_share[<=]",
        f"{_ROBUSTNESS}.walk_forward_check[G4.walk_forward.max_window_share]{_RETURNS_SOURCE}",
        SCOPE_WALK_FORWARD_WINDOWS,
        returns_check=_WALK_FORWARD,
        research_window=True,
    ),
    # ---- G1 negative controls over the window's labels ----------------------------------------
    *(
        _define(
            f"{name}_timing_p_value",
            gate,
            f"{name}_timing_p_value[>=]",
            f"{_IN_SAMPLE}[G1 {name}_control]{_VALIDATOR}",
            SCOPE_WINDOW_LABELS,
            per_instrument=True,
        )
        for name in ("shuffle", "shift")
        for gate in (f"G1.{name}_control", f"G1.{name}_control.seed.<seed>")
    ),
    *(
        _define(
            f"{name}_timing_p_value_min_over_seeds",
            f"G1.{name}_control",
            f"{name}_timing_p_value_min_over_seeds[>=]",
            f"{_IN_SAMPLE}[G1 {name}_control, control_seeds]{_VALIDATOR}",
            SCOPE_WINDOW_LABELS,
            per_instrument=True,
        )
        for name in ("shuffle", "shift")
    ),
    # ---- G2 / G3 over the Profile's walk-forward test folds ------------------------------------
    *(
        _define(
            metric,
            gate,
            label,
            f"{_IN_SAMPLE}[{gate}]{_VALIDATOR}",
            SCOPE_WALK_FORWARD_FOLDS,
            per_instrument=True,
            research_window=True,
        )
        for metric, gate, label in (
            (
                "effective_independent_trades",
                "G2.effective_sample_size",
                "effective_independent_trades[>=]",
            ),
            (
                "breakeven_cost_multiple",
                "G2.breakeven_cost_multiple",
                "breakeven_cost_multiple[>=]",
            ),
            (
                "breakeven_cost_multiple_vs_stress",
                "G2.cost_stress.<i>",
                "breakeven_cost_multiple_vs_stress[>=]",
            ),
            (
                "percentile_vs_random_entry_null",
                "G2.null_model_percentile",
                "percentile_vs_random_entry_null[>=]",
            ),
            (
                "net_mean_hac_p_greater_adjusted",
                "G3.adjusted_p_value",
                "net_mean_hac_p_greater_adjusted[<=]",
            ),
        )
    ),
    # ---- G4 checks that need re-runs of the baseline setup over the window ---------------------
    *(
        _define(metric, gate, label, f"{_ROBUSTNESS}.{function}[{gate}]{_G4_VALIDATOR}", scope)
        for metric, gate, label, function, scope in (
            (
                "probability_of_backtest_overfitting",
                "G4.overfitting",
                "probability_of_backtest_overfitting[<=]",
                "overfitting_check",
                SCOPE_PARAM_FAMILY,
            ),
            (
                "one_minus_deflated_sharpe_ratio",
                "G4.overfitting",
                "one_minus_deflated_sharpe_ratio[<=]",
                "overfitting_check",
                SCOPE_PARAM_FAMILY,
            ),
            (
                "neighbor_mean_sharpe_over_chosen_sharpe",
                "G4.param_neighborhood.performance_ratio",
                "neighbor_mean_sharpe_over_chosen_sharpe[>=]",
                "parameter_neighborhood_check",
                SCOPE_PARAM_FAMILY,
            ),
            (
                "positive_neighbor_fraction",
                "G4.param_neighborhood.positive_fraction",
                "positive_neighbor_fraction[>=]",
                "parameter_neighborhood_check",
                SCOPE_PARAM_FAMILY,
            ),
            (
                "shifted_sharpe_over_base_sharpe",
                "G4.time_alignment.<i>",
                "shifted_sharpe_over_base_sharpe[>=]",
                "time_alignment_check",
                SCOPE_RERUN,
            ),
            (
                "delayed_breakeven_cost_multiple",
                "G4.delay_stress",
                "delayed_breakeven_cost_multiple[>=]",
                "delay_stress_check",
                SCOPE_RERUN,
            ),
            (
                "undersampled_state_pnl_share",
                "G4.state.undersampled_pnl_share",
                "undersampled_state_pnl_share[<=]",
                "state_decomposition_check",
                SCOPE_WINDOW,
            ),
            (
                "capacity_notional",
                "G4.capacity.required",
                "capacity_notional[>=]",
                "capacity_check",
                SCOPE_WINDOW,
            ),
            (
                "positive_instrument_fraction",
                "G4.cross_asset.positive_fraction",
                "positive_instrument_fraction[>=]",
                "cross_asset_check",
                SCOPE_RERUN,
            ),
            (
                "positive_subuniverse_fraction",
                "G4.cross_asset.positive_fraction",
                "positive_subuniverse_fraction[>=]",
                "cross_asset_check",
                SCOPE_RERUN,
            ),
        )
    ),
)

#: The closed registry by metric name (ADR-0098 §3). A Profile metric not listed here, or listed
#: but not for the gate its baseline cites, is ``metric_undefined``.
MONITORING_METRICS: Final[Mapping[str, tuple[MonitoringMetricDefinition, ...]]] = MappingProxyType(
    {
        name: tuple(d for d in MONITORING_METRIC_DEFINITIONS if d.metric_name == name)
        for name in sorted({d.metric_name for d in MONITORING_METRIC_DEFINITIONS})
    }
)

#: Baseline gates that report an exact metric but can never be recomputed same-source-same-params
#: on an observation window (always ``metric_undefined``, with this reason).
REFUSED_METRIC_GATES: Final[tuple[tuple[str, str], ...]] = (
    (
        "G5.",
        "a sealed OOS gate: the family's one-shot sealed evaluation (SealedOosVault) cannot be "
        "re-run on another window",
    ),
    (
        "G2.market_benchmark",
        "reported only: no threshold, hence no exact value to monitor",
    ),
    ("G2.inverse_control", "reported only: no threshold, hence no exact value to monitor"),
    ("G2.cost_report.", "reported only: no threshold, hence no exact value to monitor"),
)

#: ``_check_validation_binding``: ``param:<name>`` sources of the baseline report and the
#: ``RobustnessParams`` field that supplied each (``research.validation.g4.RobustnessParams``).
_PARAM_FIELDS: Final = {
    "capacity.max_participation_rate": "max_participation_rate",
    "capacity.min_capacity": "min_capacity",
    "state.max_undersampled_pnl_share": "max_undersampled_pnl_share",
    "cross_asset.min_positive_fraction": "cross_asset_min_positive_fraction",
}
#: ``profile_field_missing:<name>`` gates and the ``RobustnessParams`` field that must be ``None``.
_MISSING_PARAM_FIELDS: Final = {
    **_PARAM_FIELDS,
    "cscv_partitions": "cscv_partitions",
    "capacity.impact_coefficient": "impact_coefficient",
}


def _refused_reason(gate_id: str | None) -> str | None:
    if gate_id is None:
        return None
    for prefix, reason in REFUSED_METRIC_GATES:
        if gate_id.startswith(prefix):
            return reason
    return None


def _ruled_definitions(
    profile: ValidationProfile, baseline: BaselineMetricSet
) -> tuple[RuledMetric, ...]:
    """Every metric the Profile rules, bound to the one definition for the gate its baseline
    cites; anything else is ``metric_undefined`` (never a guessed rule)."""
    try:
        monitor = DegradationMonitor.from_profile(profile)
    except ValueError as exc:
        raise AuthorityRefused(
            METRIC_REFUSED, f"the Profile's degradation thresholds: {exc}"
        ) from exc
    gate_ids = dict(baseline.gate_ids)
    ruled: list[RuledMetric] = []
    for metric in sorted({rule.metric for rule in monitor.rules}):
        gate_id = gate_ids.get(metric)
        refused = _refused_reason(gate_id)
        if refused is not None:
            raise AuthorityRefused(
                METRIC_UNDEFINED, f"{metric!r} of baseline gate {gate_id!r}: {refused}"
            )
        candidates = MONITORING_METRICS.get(metric, ())
        if not candidates:
            raise AuthorityRefused(
                METRIC_UNDEFINED,
                f"the Profile rules {metric!r}, which the closed metric registry "
                f"{METRIC_REGISTRY_ID} does not define",
            )
        if gate_id is None:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH, f"the baseline set cites no gate for {metric!r}"
            )
        matching = [definition for definition in candidates if definition.matches(gate_id)]
        if len(matching) != 1:
            raise AuthorityRefused(
                METRIC_UNDEFINED,
                f"{METRIC_REGISTRY_ID} defines {metric!r} for "
                f"{sorted(d.baseline_gate_id for d in candidates)}, not for baseline gate "
                f"{gate_id!r}",
            )
        ruled.append(RuledMetric(definition=matching[0], gate_id=gate_id))
    return tuple(ruled)


def _check_window_scope(
    ruled: Sequence[RuledMetric], profile: ValidationProfile, window: ObservationWindow
) -> None:
    """A definition over the Profile's fixed walk-forward folds / windows needs the observation
    window inside the Profile's research window; outside it the metric would need data outside
    the window (the research window's), so it is ``metric_undefined``, never a new window rule."""
    split = profile.data_split
    start = midnight_utc(split.research_window_start)
    boundary = midnight_utc(split.sealed_oos_boundary)
    for item in ruled:
        definition = item.definition
        if definition.requires_research_window and not (
            start <= window.start and window.end <= boundary
        ):
            raise AuthorityRefused(
                METRIC_UNDEFINED,
                f"{definition.definition_ref} ({definition.window_scope}) needs the window inside "
                f"the Profile's research window [{_utc_text(start)}, {_utc_text(boundary)}); "
                f"the window [{_utc_text(window.start)}, {_utc_text(window.end)}) is not",
            )


def _check_baseline_gates(report: ValidationReport, ruled: Sequence[RuledMetric]) -> None:
    for item in ruled:
        definition = item.definition
        gates = [gate for gate in report.gates if gate.gate_id == item.gate_id]
        if len(gates) != 1 or gates[0].metric != definition.baseline_gate_metric:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH,
                f"the report has no single {item.gate_id!r} gate reporting "
                f"{definition.baseline_gate_metric!r}",
            )
        if gates[0].value_exact is None:
            raise AuthorityRefused(
                BASELINE_BINDING_MISMATCH, f"gate {item.gate_id!r} has no exact value"
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
    as_of: datetime,
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
    view = manifest.point_in_time.simulation_time
    if view is None or view < as_of:
        raise AuthorityRefused(
            SOURCE_INCOMPLETE,
            "the dataset's point-in-time view "
            + ("is an interval" if view is None else f"{_utc_text(view)} is before as_of")
            + f" {_utc_text(as_of)}; its bars cannot be read as of that time",
        )
    return manifest


def _member_symbol(member: object) -> str | None:
    """The Canonical symbol a universe member names, or ``None`` when it names none (module
    docs: only a ``DegradedEpisodeKey`` of a known instrument's venue, type and symbol)."""
    if not isinstance(member, UniverseMember):
        return None
    episode = member.episode
    if not isinstance(episode, DegradedEpisodeKey):
        return None
    instrument = _CANONICAL_INSTRUMENTS.get(episode.symbol)
    if (
        instrument is None
        or episode.venue != instrument.venue
        or episode.instrument_type.value != instrument.instrument_type
    ):
        return None
    return episode.symbol


def _universe_symbols(
    catalog: DatasetCatalog, manifest: AnyDatasetManifest, what: str
) -> frozenset[str]:
    """The Canonical symbols of ``manifest``'s universe members (the v2 tuple, or the v3
    ``members`` evidence stream read with the catalog's verifier). A member naming no Canonical
    symbol is ``source_scope_mismatch`` (never guessed); a stream that does not verify is
    ``source_unavailable``."""
    symbols: set[str] = set()
    unnamed = 0

    def take(members: Any) -> None:
        nonlocal unnamed
        for member in members:
            symbol = _member_symbol(member)
            if symbol is None:
                unnamed += 1
            else:
                symbols.add(symbol)

    try:
        if isinstance(manifest, ResearchDatasetManifest):
            take(manifest.members)
        else:
            verifier = catalog.evidence_verifier
            if verifier is None:
                raise AuthorityRefused(
                    SOURCE_UNAVAILABLE, "a v3 manifest's members need the evidence verifier"
                )
            with iter_manifest_evidence(verifier, manifest, EvidenceStream.MEMBERS) as records:
                take(records)
    except AuthorityRefused:
        raise
    except (
        CatalogIntegrityError,
        DatasetBindingError,
        EvidenceError,
        ManifestFormError,
        ValidationError,
    ) as exc:
        raise AuthorityRefused(
            SOURCE_UNAVAILABLE, f"the {what} manifest's members do not verify: {exc}"
        ) from exc
    if unnamed:
        raise AuthorityRefused(
            SOURCE_SCOPE_MISMATCH,
            f"{unnamed} member(s) of the {what} manifest name no Canonical instrument (only a "
            "DegradedEpisodeKey of a known venue, type and symbol does)",
        )
    return frozenset(symbols)


def universe_symbols(catalog: DatasetCatalog, manifest_hash: str) -> tuple[str, ...]:
    """The sorted Canonical symbols of the verified manifest ``manifest_hash``'s universe members,
    by exactly the rule the resolver's instrument-scope check uses (修订 2 §3); refusals as there.
    Public for an environment factory that declares its decision pipeline's instruments."""
    manifest = _load_manifest(catalog, manifest_hash, "baseline")
    return tuple(sorted(_universe_symbols(catalog, manifest, "baseline")))


def _check_instruments(
    identity: TargetSourceIdentity,
    catalog: DatasetCatalog,
    *,
    baseline_manifest: AnyDatasetManifest,
    source_manifest: ResearchDatasetEvidenceManifest,
) -> None:
    """The declared instruments equal both manifests' universe members (ADR-0098 修订 2 §3)."""
    declared = frozenset(identity.instruments)
    for what, manifest in (("baseline", baseline_manifest), ("source", source_manifest)):
        members = _universe_symbols(catalog, manifest, what)
        if declared != members:
            raise AuthorityRefused(
                SOURCE_SCOPE_MISMATCH,
                f"the decision pipeline's instruments {sorted(declared)} are not the {what} "
                f"manifest's universe members {sorted(members)}",
            )


def _window_bars(
    catalog: DatasetCatalog,
    manifest_hash: str,
    instruments: tuple[str, ...],
    window: ObservationWindow,
    as_of: datetime,
) -> tuple[DatasetPriceBars, tuple[PriceBar, ...]]:
    """The window's proven bars as of ``as_of`` (module docs): read with
    ``price_cutoff=as_of``, keep ``interval_end <= window.end``, and require the kept bars of every
    instrument to tile ``[window.start, window.end)`` exactly. Returns the proven read (the
    window validation's ``dataset_bars``) and the kept bars."""
    try:
        prices = backtest_bars_from_dataset(
            catalog.adapter,
            catalog.storage,
            builder=catalog.builder,
            manifest_content_hash=manifest_hash,
            symbols=instruments,
            price_cutoff=as_of,
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
    if prices.price_cutoff != as_of:
        raise AuthorityRefused(SOURCE_UNAVAILABLE, "the bars are read at another price cutoff")
    for bar in prices.bars:
        if bar.instrument not in instruments:
            raise AuthorityRefused(SOURCE_UNAVAILABLE, f"an unrequested {bar.instrument} bar")
        if bar.available_time > as_of:
            raise AuthorityRefused(
                SOURCE_INCOMPLETE, f"a {bar.instrument} bar is available after as_of"
            )
        if not window.contains(bar.interval_start):
            raise AuthorityRefused(SOURCE_UNAVAILABLE, f"a {bar.instrument} bar outside the window")
    # a bar that starts inside the window but ends after it is not part of the window
    kept = tuple(bar for bar in prices.bars if bar.interval_end <= window.end)
    for instrument in instruments:
        mine = sorted(
            (bar for bar in kept if bar.instrument == instrument),
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
    return prices, kept


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
    #: the decision grid the pipeline evaluates on (step and warm-up) and the backtest's initial
    #: equity; each must equal the baseline run's recorded value (``_check_execution``)
    decision_step: timedelta
    decision_warmup: timedelta
    initial_equity: Decimal

    def payload(self) -> dict[str, Any]:
        return {
            "strategy_ref": str(self.strategy_ref),
            "strategy_spec_hash": self.strategy_spec_hash,
            "risk_policy_ref": None if self.risk_policy_ref is None else str(self.risk_policy_ref),
            "risk_policy_hash": self.risk_policy_hash,
            "params": _json_ready(dict(self.params)),
            "plugins": dict(sorted(self.plugins.items())),
            "decision_step_microseconds": self.decision_step // timedelta(microseconds=1),
            "decision_warmup_microseconds": self.decision_warmup // timedelta(microseconds=1),
            "initial_equity": str(self.initial_equity),
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
    # canonical JSON, not Python equality: True == 1 == 1.0 would pass (修订 2 §4); the baseline's
    # strategy params exclude its run inputs record (ADR-0100 修订 2, checked below)
    baseline_params = strategy_params(repro.params)
    try:
        same_params = canonical_json(dict(identity.params)) == canonical_json(baseline_params)
    except (TypeError, ValueError) as exc:
        raise AuthorityRefused(
            EXECUTION_MISMATCH, f"the strategy params are not canonical JSON: {exc}"
        ) from exc
    if not same_params:
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
    _check_execution_grid(identity, equity, run)
    return descriptor, identity


def _run_inputs(run: ExperimentRun, code: str) -> RunInputs | None:
    """The baseline run's ``hlens.p11.inputs@1.0.0`` record (``repro.params[RUN_INPUTS_KEY]``,
    bound into its ``experiment_hash``; ADR-0100 修订 2), or ``None`` when the run carries none — a
    run from before the amendment, never backfilled or inferred (H3 / H6). A record that is not
    exactly a valid canonical payload proves nothing and is refused with ``code``."""
    try:
        return recorded_run_inputs(run.repro.params)
    except RunInputsError as exc:
        raise AuthorityRefused(
            code, f"the baseline run's {RUN_INPUTS_KEY} record is not valid ({exc})"
        ) from exc


def _check_execution_grid(
    identity: TargetSourceIdentity, equity: Decimal, run: ExperimentRun
) -> None:
    """The declared decision grid and initial equity must equal the baseline run's recorded
    values (its ``hlens.p11.inputs@1.0.0`` ``execution`` block, compared as canonical JSON);
    unrecorded is refused (``execution_unrecorded``), never assumed."""
    step, warmup = identity.decision_step, identity.decision_warmup
    declared = identity.initial_equity
    if (
        not isinstance(step, timedelta)
        or step <= timedelta(0)
        or not isinstance(warmup, timedelta)
        or warmup < timedelta(0)
        or not isinstance(declared, Decimal)
        or not declared.is_finite()
    ):
        raise AuthorityRefused(
            EXECUTION_MISMATCH, "the target source declares no valid decision grid / equity"
        )
    if declared != equity or str(declared) != str(equity):
        raise AuthorityRefused(
            EXECUTION_MISMATCH,
            "the target source's initial equity is not the window execution's",
        )
    recorded = _run_inputs(run, EXECUTION_UNRECORDED)
    if recorded is None:
        raise AuthorityRefused(
            EXECUTION_UNRECORDED,
            f"the baseline run records no decision step, decision warm-up or initial equity (no "
            f"{RUN_INPUTS_KEY} record: a run from before ADR-0100 修订 2, never backfilled), so "
            f"the window run's (step {step}, warm-up {warmup}, equity {declared}) cannot be "
            "proven to be the baseline's",
        )
    try:
        declared_payload = execution_payload(step, warmup, declared)
    except RunInputsError as exc:
        raise AuthorityRefused(
            EXECUTION_MISMATCH, f"the target source's decision grid / equity: {exc}"
        ) from exc
    if canonical_json(declared_payload) != canonical_json(recorded.execution_payload()):
        raise AuthorityRefused(
            EXECUTION_MISMATCH,
            "the decision grid / initial equity is not the one the baseline run recorded "
            f"(declared {canonical_json(declared_payload)}, recorded "
            f"{canonical_json(recorded.execution_payload())})",
        )


def _window_returns(
    execution: WindowExecution,
    descriptor: BacktestProviderDescriptor,
    identity: TargetSourceIdentity,
    bars: tuple[PriceBar, ...],
    window: ObservationWindow,
) -> tuple[PeriodReturns, BacktestResult, dict[str, str]]:
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
    except (ValidationError, ValueError, TypeError, BacktestProviderError) as exc:
        raise AuthorityRefused(EXECUTION_MISMATCH, f"the window backtest: {exc}") from exc
    if any(not (window.start < moment <= window.end) for moment in returns.times):
        raise AuthorityRefused(EXECUTION_MISMATCH, "an equity point lies outside the window")
    return returns, result, {
        "backtest_provider": descriptor.plugin_key,
        "backtest_provider_hash": descriptor.content_hash(),
        "cost_model_ref": str(execution.cost_model.ref),
        "cost_model_hash": execution.cost_model.content_hash(),
        "initial_equity": str(execution.initial_equity),
        "backtest_request_hash": request.content_hash(),
        "backtest_result_hash": result.result_hash,
    }


# ======================================================================================
# 3c. the window re-run of the baseline validator (``window_validation`` definitions)
# ======================================================================================

#: A decision-pipeline factory over the window's proven bars (the ``TrialRunner`` the baseline
#: validator re-ran its trials with, rebuilt on the window).
TrialRunnerFactory = Callable[[tuple[PriceBar, ...], ObservationWindow], TrialRunner]
#: A causal state labeller over the window's proven bars (the baseline ``ValidatorSetup.state_of``).
StateLabelFactory = Callable[[tuple[PriceBar, ...], ObservationWindow], Callable[[datetime], str]]

#: ``G1.<control>.seed.<s>`` (optionally per instrument): the baseline's control seeds.
_CONTROL_SEED_GATE: Final = re.compile(
    r"^G1\.(?:shuffle|shift)_control\.seed\.(-?(?:0|[1-9][0-9]*))" + _INSTRUMENT_SUFFIX + "$"
)
#: Structural gates of the window validation whose ``FAIL`` means the binding does not describe
#: the window run, or the re-run broke an information-flow rule (a refusal of **every** metric of
#: that re-run, never evidence about the strategy). Matched on the base gate id and on its
#: per-instrument variants (``<id>.instrument.<name>``).
_STRUCTURAL_GATES: Final = {
    "G0.backtest_cost_model": EXECUTION_MISMATCH,
    "G0.execution_model": EXECUTION_MISMATCH,
    "G0.manifest_binding": EXECUTION_MISMATCH,
    "G0.reproducibility": EXECUTION_MISMATCH,
    "G0.signal_determinism": EXECUTION_MISMATCH,
    "G0.bindings": BASELINE_BINDING_MISMATCH,
    "G0.run_state": BASELINE_BINDING_MISMATCH,
    # C-L2 / C-S1 information-flow gates: an Outcome among the inputs, sides that move with the
    # label values, or labels touching the sealed OOS window make the whole re-run unusable
    "G1.outcome_not_input": EXECUTION_MISMATCH,
    "G1.label_blind_sides": EXECUTION_MISMATCH,
    "G1.sealed_oos_excluded": SOURCE_SCOPE_MISMATCH,
}
_STRUCTURAL_GATE_ID: Final = re.compile(
    "^(" + "|".join(re.escape(gate) for gate in _STRUCTURAL_GATES) + ")" + _INSTRUMENT_SUFFIX + "$"
)
#: Everything the validator's re-run may raise besides a deliberate refusal handled first.
_RUN_ERRORS: Final = (
    ValidationError,
    ValueError,
    TypeError,
    BacktestProviderError,
    StrategyProviderError,
    RiskProviderError,
)


@dataclass(frozen=True)
class WindowValidationBinding:
    """The baseline ``ValidatorSetup``'s inputs that the reproducibility tuple does not carry,
    so that the ``window_validation`` definitions can re-run the **same** validator with the
    **same** parameters on the window (module docs, **3. Metrics**):

    - ``spec``: the baseline ``StrategySpec`` (ref + content hash = the run's);
    - ``metadata``: the baseline ``ExperimentMetadata`` (``family_trial_count``, ``trial_index``,
      the Profile / experiment bindings = the run's);
    - ``label_spec``: the Outcome definition (ref + spec hash = the run's ``outcome_ref``);
    - ``outcome_provider``: its provider (``name@version`` + descriptor hash = the run's
      ``plugin_versions`` entry); labels are evaluation targets only, never inputs (C-L2);
    - ``trials``: builds the ``TrialRunner`` over the window's proven bars; its re-run of the
      chosen point must reproduce the window backtest exactly (``G0.reproducibility``);
    - ``seed`` (one of the run's ``seeds``), ``robustness`` (the ``RobustnessParams``; checked
      against every ``param:`` / ``profile_field_missing:`` gate of the baseline report),
      ``control_seeds`` (checked against the report's per-seed control gates),
      ``market_benchmark`` (required when the report carries ADR-0060 items), ``backtester`` /
      ``execution`` (declared exactly when the report carries ``G0.execution_model``),
      ``declared_instruments`` (the C-R3 scope: exactly the pipeline's instruments);
    - **recorded inputs** (ADR-0100 修订 2): a baseline run with a valid ``hlens.p11.inputs@1.0.0``
      record (``research.experiments.run_inputs``, in its hashed ``repro.params``) records the
      validator's ``family_trial_count``, validation ``seed``, ``control_seeds``,
      ``cscv_partitions``, ``impact_coefficient`` and state labeller identity. Each is compared
      exactly (canonical JSON) with the binding's ``metadata.family_trial_count``, ``seed``,
      ``control_seeds``, ``robustness.cscv_partitions`` / ``.impact_coefficient`` and — when
      ``state_labels`` is given — ``state_labels_identity``; any difference is
      ``baseline_binding_mismatch``, and only when all are equal are the dependencies below
      proven (no ``baseline_input_unrecorded``);
    - **unrecorded inputs** (integrity fixes 2026-09-30): a baseline run **without** that record
      (every run from before the amendment; never backfilled or inferred) records neither
      ``metadata.family_trial_count``, nor which of the run's ``seeds`` the validator used, nor a
      ``param:`` ``cscv_partitions`` / ``impact_coefficient`` value, nor the state labeller (nor
      does the baseline report). A ruled definition whose gate depends on one of them is refused
      (``baseline_input_unrecorded``, ``_check_unrecorded_inputs``): ``G3.*`` and
      ``G4.overfitting`` (family trial count; G3 also the seed through the G2 null model),
      ``G2.null_model_percentile`` (seed), the single-seed ``G1`` controls and — through the G1
      stage stop — every ``G2.*`` gate when the baseline ran single-seed controls (seed),
      ``G4.overfitting`` with a ``param:`` CSCV partition count, ``G4.capacity.*`` with a
      ``param:`` impact coefficient, and ``G4.state.*`` with a state labeller;
    - ``state_labels``: the baseline's causal state labeller over the window (``None``: the C-R2
      gates are the function's own ``state_labels_missing``, i.e. *missing*), with
      ``state_labels_identity`` its declared identity (a JSON object; for the research loop,
      ``research.experiments.run_inputs.state_labeller_identity``), given exactly with
      ``state_labels``. Like ``TargetSourceIdentity`` it is caller code bound by its declared
      identity, not re-derived here;
    - ``bar_volume``: ``True`` when the baseline read bar volumes — the window's are the proven
      bars' own ``volume`` (the source ADR-0064 requires the volumes to equal); ``False``: the
      capacity gates are the function's own ``bar_volume_missing``, i.e. *missing*.
    """

    spec: StrategySpec
    metadata: ExperimentMetadata
    label_spec: OutcomeLabelSpec
    outcome_provider: OutcomeProvider
    trials: TrialRunnerFactory
    seed: int
    robustness: RobustnessParams
    declared_instruments: tuple[str, ...]
    state_labels: StateLabelFactory | None = None
    bar_volume: bool = False
    backtester: BacktestProvider | None = None
    execution: ExecutionModel | None = None
    control_seeds: tuple[int, ...] | None = None
    market_benchmark: bool = False
    state_labels_identity: Mapping[str, Any] | None = None

    def declared_backtester_hash(self) -> str | None:
        if self.backtester is not None:
            return self.backtester.descriptor.content_hash()
        if self.execution is not None:
            return BarBacktester(execution=self.execution).descriptor.content_hash()
        return None

    def payload(self) -> dict[str, Any]:
        descriptor = self.outcome_provider.descriptor
        return {
            "strategy_spec_hash": self.spec.content_hash(),
            "experiment_metadata_hash": self.metadata.content_hash(),
            "family_trial_count": self.metadata.family_trial_count,
            "label_spec_hash": self.label_spec.content_hash(),
            "outcome_provider": f"{descriptor.name}@{descriptor.version}",
            "outcome_provider_hash": descriptor.content_hash(),
            "seed": self.seed,
            "robustness": _json_ready(self.robustness.to_dict()),
            "declared_instruments": list(self.declared_instruments),
            "state_labels": self.state_labels is not None,
            "bar_volume": "proven_bar_volume" if self.bar_volume else None,
            "declared_backtester_hash": self.declared_backtester_hash(),
            "control_seeds": None if self.control_seeds is None else list(self.control_seeds),
            "market_benchmark": self.market_benchmark,
            "state_labels_identity": (
                None
                if self.state_labels_identity is None
                else _json_ready(dict(self.state_labels_identity))
            ),
        }


def _binding_mismatch(message: str) -> AuthorityRefused:
    return AuthorityRefused(BASELINE_BINDING_MISMATCH, f"the window validation binding: {message}")


def _check_validation_binding(
    binding: WindowValidationBinding,
    *,
    subject: Ref,
    profile: ValidationProfile,
    report: ValidationReport,
    run: ExperimentRun,
    identity: TargetSourceIdentity,
    definitions: Sequence[RuledMetric],
) -> None:
    """The binding must describe the baseline validation (class docs); else refused."""
    if not isinstance(binding, WindowValidationBinding):
        raise AuthorityRefused(EXECUTION_MISMATCH, "validation must be a WindowValidationBinding")
    repro = run.repro
    spec, strategy = binding.spec, repro.strategy_ref
    if (
        not isinstance(spec, StrategySpec)
        or strategy is None
        or spec.ref.target_identity() != strategy.target_identity()
        or spec.content_hash() != repro.dependency_hashes.get(str(strategy))
    ):
        raise AuthorityRefused(
            EXECUTION_MISMATCH, "the binding's StrategySpec is not the baseline run's"
        )
    if spec.ref.target_identity() != subject.target_identity():
        raise _binding_mismatch("the StrategySpec is not the subject")
    meta = binding.metadata
    if (
        not isinstance(meta, ExperimentMetadata)
        or meta.experiment_hash != run.experiment_hash
        or meta.validation_profile_hash != profile.content_hash()
        or meta.validation_profile.target_identity() != profile.ref.target_identity()
    ):
        raise _binding_mismatch("the ExperimentMetadata is not the baseline run's")
    label, outcome = binding.label_spec, repro.outcome_ref
    if (
        not isinstance(label, OutcomeLabelSpec)
        or outcome is None
        or label.outcome.target_identity() != outcome.target_identity()
        or label.outcome_spec_hash != repro.dependency_hashes.get(str(outcome))
    ):
        raise AuthorityRefused(
            EXECUTION_MISMATCH,
            "the label spec is not the Outcome definition the baseline run recorded",
        )
    descriptor = getattr(binding.outcome_provider, "descriptor", None)
    if (
        descriptor is None
        or repro.plugin_versions.get(f"{descriptor.name}@{descriptor.version}")
        != descriptor.content_hash()
        or not descriptor.supports(label)
    ):
        raise AuthorityRefused(
            EXECUTION_MISMATCH, "the outcome provider is not the one the baseline run recorded"
        )
    if (
        meta.constitution_version != repro.constitution_version
        or meta.constitution_version != report.constitution_version
        or canonical_json(meta.profile_selection.model_dump(mode="json"))
        != canonical_json(repro.profile_selection.model_dump(mode="json"))
    ):
        raise _binding_mismatch(
            "the ExperimentMetadata's Constitution version / Profile selection is not the "
            "baseline run's and report's"
        )
    seed = binding.seed
    if isinstance(seed, bool) or not isinstance(seed, int) or seed not in repro.seeds:
        raise _binding_mismatch("the validation seed is not one of the baseline run's seeds")
    if not isinstance(binding.robustness, RobustnessParams):
        raise _binding_mismatch("robustness must be RobustnessParams")
    declared = binding.declared_instruments
    if (
        not isinstance(declared, tuple)
        or not declared
        or not all(isinstance(name, str) and name for name in declared)
        or len(set(declared)) != len(declared)
        or set(declared) != set(identity.instruments)
    ):
        raise _binding_mismatch(
            "declared_instruments must be exactly the pipeline's (baseline) instruments"
        )
    if not callable(binding.trials) or (
        binding.state_labels is not None and not callable(binding.state_labels)
    ):
        raise _binding_mismatch("trials / state_labels must be factories")
    labeller = binding.state_labels_identity
    if labeller is not None:
        if binding.state_labels is None or not isinstance(labeller, Mapping) or not labeller:
            raise _binding_mismatch(
                "state_labels_identity is the JSON object identity of a given state_labels"
            )
        try:
            canonical_json(dict(labeller))
        except (TypeError, ValueError) as exc:
            raise _binding_mismatch(f"state_labels_identity is not canonical JSON: {exc}") from exc
    if binding.backtester is not None and binding.execution is not None:
        raise _binding_mismatch("give either backtester or execution, not both")
    # what the baseline report itself shows about its setup
    ids = {gate.gate_id for gate in report.gates}
    declared_model = binding.backtester is not None or binding.execution is not None
    if ("G0.execution_model" in ids) != declared_model:
        raise _binding_mismatch("the declared execution model differs from the baseline setup's")
    several = len(identity.instruments) > 1
    if ("G0.instrument_scope" in ids and not several) or (
        "G0.single_instrument_adapter" in ids and several
    ):
        raise _binding_mismatch("the instrument path differs from the baseline setup's")
    benchmark = any(
        item.startswith("G2.market_benchmark") or item.startswith("G2.inverse_control")
        for item in ids
    )
    if benchmark and not binding.market_benchmark:
        raise _binding_mismatch("the baseline computed the ADR-0060 market benchmark items")
    seeds = {int(match.group(1)) for item in ids if (match := _CONTROL_SEED_GATE.fullmatch(item))}
    if seeds != set(binding.control_seeds or ()):
        raise _binding_mismatch("the control seeds differ from the baseline report's")
    params = binding.robustness
    for gate in report.gates:
        source = gate.threshold_source or ""
        if source.startswith(PARAM_SOURCE_PREFIX):
            name = _PARAM_FIELDS.get(source.removeprefix(PARAM_SOURCE_PREFIX))
            if name is not None:
                value = getattr(params, name)
                if value is None or gate.threshold is None or float(value) != gate.threshold:
                    raise _binding_mismatch(f"{source} is not the baseline's value")
        if gate.metric.startswith(f"{PROFILE_FIELD_MISSING}:"):
            name = _MISSING_PARAM_FIELDS.get(gate.metric.split(":", 1)[1])
            if name is not None and getattr(params, name) is not None:
                raise _binding_mismatch(f"the baseline had no {gate.metric.split(':', 1)[1]}")
    for path, name in _PROFILE_SOURCED_PARAMS:
        if profile_has(profile, path) and getattr(params, name) is not None:
            raise _binding_mismatch(
                f"the Profile carries {path}; the baseline could not use a param:{name}"
            )
    _check_unrecorded_inputs(
        binding, profile=profile, report=report, run=run, definitions=definitions
    )


#: Non-threshold G4 parameters a Profile may carry (then a ``param:`` value is refused by the
#: validator itself, C-A4) and the ``RobustnessParams`` field that would carry the ``param:``.
_PROFILE_SOURCED_PARAMS: Final = (
    ("significance.cscv_partitions", "cscv_partitions"),
    ("capacity.impact_coefficient", "impact_coefficient"),
)
_INSTRUMENT_TAIL: Final = re.compile(r"\.instrument\.[A-Za-z0-9_-]+$")


def _unrecorded_dependency(
    gate_id: str,
    binding: WindowValidationBinding,
    profile: ValidationProfile,
    single_seed: bool,
) -> str | None:
    """Why the window value of the baseline gate ``gate_id`` depends on a validator input the
    baseline does not record (class docs, **unrecorded inputs**), or ``None``."""
    base = _INSTRUMENT_TAIL.sub("", gate_id)
    if base.startswith("G3."):
        return (
            "G3 uses metadata.family_trial_count (multiple-testing adjustment) and runs only after "
            "the seeded G2 null model; neither the trial count nor the validation seed is recorded "
            "by the baseline report or run"
        )
    if base == "G4.overfitting":
        return (
            "the overfitting check uses metadata.family_trial_count, which the baseline report and "
            "run do not record"
        )
    if base == "G2.null_model_percentile":
        return (
            "the null model is drawn with the validation seed, which the baseline does not record"
        )
    if single_seed and base in ("G1.shuffle_control", "G1.shift_control"):
        return (
            "the baseline ran single-seed negative controls with the validation seed, which it "
            "does not record"
        )
    if single_seed and base.startswith("G2."):
        return (
            "G2 runs only when the single-seed G1 controls pass, and those are drawn with the "
            "validation seed, which the baseline does not record"
        )
    params = binding.robustness
    if base.startswith("G4.overfitting") and (
        params.cscv_partitions is not None
        and not profile_has(profile, "significance.cscv_partitions")
    ):
        return "param:cscv_partitions is not recorded by the baseline report"
    if base.startswith("G4.capacity.") and (
        params.impact_coefficient is not None
        and not profile_has(profile, "capacity.impact_coefficient")
    ):
        return "param:capacity.impact_coefficient is not recorded by the baseline report"
    if base.startswith("G4.state.") and binding.state_labels is not None:
        return (
            "the state labeller is caller code the baseline does not record (no spec hash binds "
            "it)"
        )
    return None


def _check_recorded_inputs(binding: WindowValidationBinding, recorded: RunInputs) -> None:
    """The binding's validator inputs against the baseline run's record (class docs, **recorded
    inputs**): each compared as canonical JSON (``True``, ``1`` and ``1.0`` differ); any difference
    is ``baseline_binding_mismatch``. The state labeller is compared when the binding gives one
    (without one the C-R2 gates are *missing*, never a value)."""
    robustness = binding.robustness
    declared: dict[str, Any] = {
        "family_trial_count": binding.metadata.family_trial_count,
        "validation_seed": binding.seed,
        "control_seeds": None if binding.control_seeds is None else list(binding.control_seeds),
        "cscv_partitions": robustness.cscv_partitions,
        "impact_coefficient": robustness.impact_coefficient,
    }
    if binding.state_labels is not None:
        identity = binding.state_labels_identity
        declared["state_labeller"] = None if identity is None else dict(identity)
    payload = recorded.validation_payload()
    differs: list[str] = []
    for name, value in declared.items():
        try:
            same = canonical_json(value) == canonical_json(payload[name])
        except (TypeError, ValueError):
            same = False
        if not same:
            differs.append(name)
    if differs:
        raise _binding_mismatch(
            f"{sorted(differs)} are not the values the baseline run recorded in {RUN_INPUTS_KEY}"
        )


def _check_unrecorded_inputs(
    binding: WindowValidationBinding,
    *,
    profile: ValidationProfile,
    report: ValidationReport,
    run: ExperimentRun,
    definitions: Sequence[RuledMetric],
) -> None:
    """With the baseline run's ``hlens.p11.inputs@1.0.0`` record, every recorded input must equal
    the binding's (``_check_recorded_inputs``) and then no dependency is unrecorded. Without it
    (an old run: never backfilled or inferred), refuse every ruled ``window_validation``
    definition whose gate depends on an input the baseline does not record (class docs); the
    first one is the refusal. An invalid record is ``baseline_input_unrecorded`` too."""
    recorded = _run_inputs(run, BASELINE_INPUT_UNRECORDED)
    if recorded is not None:
        _check_recorded_inputs(binding, recorded)
        return
    ids = {gate.gate_id for gate in report.gates}
    single_seed = not any(_CONTROL_SEED_GATE.fullmatch(item) for item in ids)
    for item in definitions:
        if item.definition.evidence != EVIDENCE_WINDOW_VALIDATION:
            continue
        reason = _unrecorded_dependency(item.gate_id, binding, profile, single_seed)
        if reason is not None:
            raise AuthorityRefused(
                BASELINE_INPUT_UNRECORDED,
                f"{item.definition.definition_ref} @ {item.gate_id}: {reason}; the binding's "
                "value cannot be proven to be the baseline's",
            )


def _window_validation_gates(
    binding: WindowValidationBinding,
    *,
    subject: Ref,
    profile: ValidationProfile,
    report: ValidationReport,
    run: ExperimentRun,
    cost_model: CostModelSpec,
    identity: TargetSourceIdentity,
    prices: DatasetPriceBars,
    bars: tuple[PriceBar, ...],
    window: ObservationWindow,
    as_of: datetime,
    backtest: BacktestResult,
    robustness: bool,
) -> dict[str, GateResult]:
    """The baseline validator re-run on the window (module docs): ``validate`` for G0 – G3 (its
    own stage-stop semantics: a gate of a stage that did not run is *missing*) and, when a G4
    metric is ruled, ``robustness_diagnostic`` (G4 regardless of G0 – G3, report only). The
    context is the baseline's (report id, run, metadata, Profile, cost model, label spec) stamped
    ``as_of`` (no clock); the data are the window's proven bars only."""
    instruments = tuple(sorted(identity.instruments))
    context = ValidationContext(
        report_id=report.report_id,
        subject=subject,
        run=run,
        metadata=binding.metadata,
        profile=profile,
        cost_model=cost_model,
        label_spec=binding.label_spec,
        created_at=as_of,
    )
    try:
        setup = ValidatorSetup(
            context=context,
            outcome_provider=binding.outcome_provider,
            manifest_content_hash=prices.manifest_content_hash,
            instrument=instruments[0],
            trials=binding.trials(bars, window),
            chosen_params=strategy_params(run.repro.params),
            seed=binding.seed,
            robustness=binding.robustness,
            state_of=None if binding.state_labels is None else binding.state_labels(bars, window),
            bar_volume=(
                {(bar.instrument, bar.interval_start): bar.volume for bar in bars}
                if binding.bar_volume
                else None
            ),
            declared_instruments=binding.declared_instruments,
            dataset_bars=prices,
            backtester=binding.backtester,
            execution=binding.execution,
            control_seeds=binding.control_seeds,
            instruments=None if len(instruments) == 1 else instruments,
            market_benchmark=binding.market_benchmark,
        )
        validator = PipelineBacktestValidator(setup)
        answer = validator.validate(subject, binding.spec, backtest)
        gates = {gate.gate_id: gate for gate in answer.report.gates}
        for gate_id, gate in sorted(gates.items()):
            match = _STRUCTURAL_GATE_ID.fullmatch(gate_id)
            if match is not None and gate.verdict is Verdict.FAIL:
                raise AuthorityRefused(
                    _STRUCTURAL_GATES[match.group(1)],
                    f"the window validation's {gate_id} failed: the binding does not "
                    "describe the window run (every metric of this re-run is refused)",
                )
        if robustness:
            diagnostic = validator.robustness_diagnostic(binding.spec, backtest)
            gates.update({gate.gate_id: gate for gate in diagnostic.gates})
    except AuthorityRefused:
        raise
    except OutcomeUsedAsInput as exc:  # C-L2: an Outcome among the inputs is never a metric
        raise AuthorityRefused(EXECUTION_MISMATCH, f"the window validation: {exc}") from exc
    except _RUN_ERRORS as exc:
        raise AuthorityRefused(METRIC_REFUSED, f"the window validation: {exc}") from exc
    return gates


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
    baseline_set_hash: str
    validation_report_hash: str
    profile_ref: str
    profile_hash: str
    metric_definitions: tuple[RuledMetric, ...]
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
                "baseline_set_hash": self.baseline_set_hash,
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
    window execution and the baseline run / dataset, and — for the ``window_validation`` metric
    definitions only — the baseline validator's own inputs (``validation``; ``None``: those
    metrics are ``metric_inputs_unavailable``). Supplied by an embedding caller or a trusted
    ``--authority-environment`` factory (e.g.
    ``research.operations.authority_environment:default_environment``)."""

    catalog: DatasetCatalog
    execution: WindowExecution
    baseline_run: ExperimentRun
    baseline_manifest_hash: str
    validation: WindowValidationBinding | None = None


def _resolve_lifecycle(
    lifecycle: LifecycleRegistry,
    head: LifecycleHead,
    subject: Ref,
    window: ObservationWindow,
    as_of: datetime,
) -> tuple[LifecycleHead, LifecycleHistory, tuple[str, ...]]:
    """The subject's replay at ``head`` truncated to ``occurred_at <= as_of`` (module docs,
    ADR-0098 修订 2 §2), with the hashes of exactly the kept records."""
    if not isinstance(lifecycle, LifecycleRegistry):
        raise AuthorityRefused(LIFECYCLE_UNAVAILABLE, "lifecycle must be an open LifecycleRegistry")
    try:
        if not lifecycle.read_only:
            raise AuthorityRefused(
                LIFECYCLE_UNAVAILABLE,
                "the resolver reads a read-only snapshot (LifecycleRegistry.open_snapshot), not a "
                "writer instance",
            )
        pinned = lifecycle.verify_head(head)
        replayed = lifecycle.lifecycle_of(subject, pinned)
    except UnknownHead as exc:
        raise AuthorityRefused(LIFECYCLE_HEAD_UNKNOWN, str(exc)) from exc
    except RegistryError as exc:
        raise AuthorityRefused(LIFECYCLE_UNAVAILABLE, str(exc)) from exc
    transitions = replayed.history.transitions
    if len(replayed.record_hashes) != len(transitions):
        raise AuthorityRefused(
            LIFECYCLE_UNAVAILABLE, "the replay's record hashes do not match its transitions"
        )
    kept = 0  # occurred_at is non-decreasing (LifecycleHistory), so the kept ones are a prefix
    while kept < len(transitions) and transitions[kept].occurred_at <= as_of:
        kept += 1
    try:
        history = LifecycleHistory(subject=replayed.history.subject, transitions=transitions[:kept])
    except (ValidationError, ValueError) as exc:
        raise AuthorityRefused(
            LIFECYCLE_UNAVAILABLE, f"the truncated replay does not validate: {exc}"
        ) from exc
    where = f"at head {pinned.record_count}/{pinned.last_record_hash} as of {_utc_text(as_of)}"
    if history.current_state is not LifecycleState.ACTIVE:
        raise AuthorityRefused(
            LIFECYCLE_NOT_ACTIVE, f"{subject} replays to {history.current_state} {where}"
        )
    entered = history.transitions[-1]  # the transition into the terminal ACTIVE state
    if entered.occurred_at > window.start:
        raise AuthorityRefused(
            LIFECYCLE_NOT_ACTIVE,
            f"{subject} entered ACTIVE at {_utc_text(entered.occurred_at)}, after the window "
            f"start {_utc_text(window.start)} ({where})",
        )
    moved = [t for t in history.transitions if window.start < t.occurred_at <= as_of]
    if moved:
        raise AuthorityRefused(
            LIFECYCLE_NOT_ACTIVE,
            f"{subject} has {len(moved)} lifecycle transition(s) in (window start, as_of] "
            f"(first at {_utc_text(moved[0].occurred_at)}; {where})",
        )
    return pinned, history, replayed.record_hashes[:kept]


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
    as_of: datetime,
    validation: WindowValidationBinding | None = None,
) -> AuthorityResolution:
    """Resolve ``run_degradation_check``'s lifecycle and recent inputs from the ADR-0098
    authorities (module docs) as of the explicit ``as_of`` (UTC, ``>= window.end``; ADR-0098
    修订 2 §1). ``lifecycle`` must be a read-only snapshot (``LifecycleRegistry.open_snapshot``).
    ``validation`` (ADR-0100 item 3) is needed only when a ruled metric's definition re-runs the
    baseline validator on the window (``window_validation``). Raises ``AuthorityRefused`` (with a
    ``code``) on any failed rule; reads only, writes nothing, reads no clock."""
    if not isinstance(subject, Ref):
        raise DegradationOperationRefused("subject must be a Ref")
    if not isinstance(window, ObservationWindow):
        raise DegradationOperationRefused("window must be an ObservationWindow")
    if (
        not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() != UTC.utcoffset(None)
    ):
        raise DegradationOperationRefused("as_of must be a timezone-aware UTC datetime")
    as_of = as_of.astimezone(UTC)
    if as_of < window.end:
        raise DegradationOperationRefused("as_of must not be before the window end")
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

    # 1. lifecycle at the explicit head, truncated to as_of
    pinned, history, record_hashes = _resolve_lifecycle(lifecycle, head, subject, window, as_of)
    anchor = ANCHOR_PRESENT if lifecycle.anchored else ANCHOR_ABSENT

    # 3a. every ruled metric is defined for the gate its baseline cites, over a window its
    # definition can use, and its inputs are supplied
    definitions = _ruled_definitions(profile, baseline)
    _check_baseline_gates(baseline_report, definitions)
    _check_window_scope(definitions, profile, window)
    rerunning = sorted(
        item.definition.definition_ref
        for item in definitions
        if item.definition.evidence == EVIDENCE_WINDOW_VALIDATION
    )
    needs_validation = bool(rerunning)
    if needs_validation and validation is None:
        raise AuthorityRefused(
            METRIC_INPUTS_UNAVAILABLE,
            f"{rerunning} re-run the baseline validator on the window and need a "
            "WindowValidationBinding",
        )

    # the baseline run, its dataset and the execution identities
    _check_baseline_run(subject, profile, baseline_report, baseline_run)
    baseline_manifest = _load_manifest(catalog, baseline_manifest_hash, "baseline")
    if baseline_manifest.dataset not in baseline_run.repro.dataset_snapshots:
        raise AuthorityRefused(
            SOURCE_SCOPE_MISMATCH,
            "the baseline manifest's dataset is not one the baseline run read",
        )
    descriptor, identity = _check_execution(execution, baseline_run)
    # the record _check_execution has just verified (a run without one is refused there)
    run_inputs = _run_inputs(baseline_run, EXECUTION_UNRECORDED)
    if validation is not None and needs_validation:
        _check_validation_binding(
            validation,
            subject=subject,
            profile=profile,
            report=baseline_report,
            run=baseline_run,
            identity=identity,
            definitions=definitions,
        )

    # 2. the source and the window's proven bars
    manifest = _resolve_source(
        catalog,
        dataset_id=dataset_id,
        manifest_hash=manifest_hash,
        baseline_manifest=baseline_manifest,
        window=window,
        as_of=as_of,
    )
    _check_instruments(
        identity, catalog, baseline_manifest=baseline_manifest, source_manifest=manifest
    )
    prices, bars = _window_bars(catalog, manifest_hash, identity.instruments, window, as_of)

    # returns through the provider, then 3b. each definition's validation function
    returns, backtest, window_payload = _window_returns(
        execution, descriptor, identity, bars, window
    )
    rerun: Callable[[], Mapping[str, GateResult]] | None = None
    if validation is not None and needs_validation:
        binding = validation
        robustness = any(
            item.definition.evidence == EVIDENCE_WINDOW_VALIDATION
            and item.gate_id.startswith("G4.")
            for item in definitions
        )

        def window_validation() -> Mapping[str, GateResult]:
            return _window_validation_gates(
                binding,
                subject=subject,
                profile=profile,
                report=baseline_report,
                run=baseline_run,
                cost_model=execution.cost_model,
                identity=identity,
                prices=prices,
                bars=bars,
                window=window,
                as_of=as_of,
                backtest=backtest,
                robustness=robustness,
            )

        rerun = window_validation

    inputs = _WindowInputs(profile, returns, rerun)
    values: dict[str, Decimal] = {}
    for item in definitions:
        definition = item.definition
        try:
            value = definition.compute(inputs, item.gate_id)
        except AuthorityRefused:
            raise
        except ValueError as exc:
            raise AuthorityRefused(
                METRIC_REFUSED, f"{definition.definition_ref}: {exc}"
            ) from exc
        if value is not None:
            values[definition.metric_name] = value

    source = SourceIdentity.of(manifest)
    last_bar = max(bars, key=lambda bar: (bar.interval_start, bar.instrument))
    observed = max(bar.available_time for bar in bars)
    set_id = "authority:" + content_hash(
        {
            "dataset_id": dataset_id,
            "manifest_hash": manifest_hash,
            "lifecycle_head": pinned.payload(),
            "window": window.payload(),
            "as_of": _utc_text(as_of),
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
        as_of=as_of,
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
        baseline_set_hash=baseline.content_hash(),
        validation_report_hash=baseline_report.content_hash(),
        profile_ref=str(profile.ref),
        profile_hash=profile.content_hash(),
        metric_definitions=definitions,
        execution_json=canonical_json(
            {
                "target_source": identity.payload(),
                **window_payload,
                "baseline_run_inputs": None if run_inputs is None else run_inputs.payload(),
                "window_validation": (
                    validation.payload()
                    if validation is not None and needs_validation
                    else None
                ),
            }
        ),
        recent_manifest_hash=recent.manifest_hash,
        as_of=as_of,
        window_start=window.start,
        window_end=window.end,
    )
    return AuthorityResolution(lifecycle=history, recent=recent, provenance=provenance)
