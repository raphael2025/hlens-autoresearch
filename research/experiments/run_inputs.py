"""Same-source inputs recorded by a new experiment run (ADR-0100 修订 2).

The P11 authority (``research.operations.authority``) re-computes a baseline gate on a later window
only with the **same** inputs the baseline used. The frozen ``ReproducibilityTuple`` has no field
for several of them, so every resolution used to be refused (``execution_unrecorded`` /
``baseline_input_unrecorded``). From ADR-0100 修订 2 on, a new run records them in the existing,
hashed ``repro.params`` mapping under one namespaced key, ``RUN_INPUTS_KEY``
(``hlens.p11.inputs@1.0.0``), whose value is the canonical JSON text of ``RunInputs.payload()``.
``repro.params`` is part of ``experiment_hash`` (the run's content hash), so the record is bound
to the run; no ``core/`` contract changes.

Recorded (exactly as the run and its validation use them):

- ``execution``: the decision grid of the round data (``decision_step`` / ``decision_warmup``, as
  microseconds; the grid ``research.loop.segment.decision_grid`` was built with) and the
  backtest's ``initial_equity`` (``str`` of the ``Decimal``, so ``1000`` and ``1000.00`` differ);
- ``validation``: the ``ExperimentMetadata.family_trial_count`` handed to the validator;
  the validator seed (``validation_seed``: the single-seed G1 negative controls and the G2 null
  model are drawn with it); the multi-seed G1 ``control_seeds`` (``null``: single-seed
  controls); the ``RobustnessParams`` ``cscv_partitions`` and ``impact_coefficient`` (``null``:
  not given — then a Profile field, or the declared execution model, supplies it); the identity
  of the state labeller the validator's ``state_of`` reads (``null``: none).

Rules:

- **Old runs are never backfilled or inferred** (H3 / H6): a run without the key has no record
  (``recorded_run_inputs`` returns ``None``) and the authority keeps refusing it.
- The key is reserved: ``with_run_inputs`` refuses params that already carry it (a strategy param
  cannot shadow the record), and ``strategy_params`` is the strategy's own parameters — every
  reader that treats ``repro.params`` as strategy parameters must use it.
- ``recorded_run_inputs`` is strict: a value that is not exactly the canonical JSON text of a
  valid ``hlens.p11.inputs@1.0.0`` payload raises ``RunInputsError`` (never a partial read).

Code completion (2026-09-30, CODE_COMPLETE / DEBUG_PENDING; not run, not tested).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from core.domain.base import canonical_json

__all__ = [
    "P7_PLAN_PARAM_KEY",
    "RESERVED_PARAM_KEYS",
    "RUN_INPUTS_FORMAT",
    "RUN_INPUTS_KEY",
    "STATE_LABELLER_FORMAT",
    "RunInputs",
    "RunInputsError",
    "execution_payload",
    "recorded_run_inputs",
    "state_labeller_identity",
    "strategy_params",
    "with_run_inputs",
]

#: Identity and version of the recorded payload.
RUN_INPUTS_FORMAT: Final = "hlens.p11.inputs@1.0.0"
#: The ``repro.params`` key that carries it (the format id itself: namespaced, never a strategy
#: parameter name).
RUN_INPUTS_KEY: Final = RUN_INPUTS_FORMAT
#: The other reserved ``repro.params`` record: ADR-0103's ``hlens.p7.plan@1.0.0`` (its owner,
#: ``research.hypotheses.p7_binding.P7_PLAN_KEY``, is not imported here; a test pins equality).
P7_PLAN_PARAM_KEY: Final = "hlens.p7.plan@1.0.0"
#: Every reserved record key ``strategy_params`` strips.
RESERVED_PARAM_KEYS: Final = frozenset({RUN_INPUTS_KEY, P7_PLAN_PARAM_KEY})
#: Identity of the research loop's state labeller (``state_labeller_identity``).
STATE_LABELLER_FORMAT: Final = "hlens.loop.state-labeller@1.0.0"
#: The label the loop's validator ``state_of`` returns for a decision time without a computable
#: state (``research.loop.trials.ValidationStage``).
_MISSING_LABEL: Final = "unknown"

_EXECUTION_KEYS: Final = frozenset(
    {"decision_step_microseconds", "decision_warmup_microseconds", "initial_equity"}
)
_VALIDATION_KEYS: Final = frozenset(
    {
        "family_trial_count",
        "validation_seed",
        "control_seeds",
        "cscv_partitions",
        "impact_coefficient",
        "state_labeller",
    }
)
_MICROSECOND: Final = timedelta(microseconds=1)

#: A ``repro.params`` value (``ReproducibilityTuple.params``).
type RunParam = str | int | float | bool


class RunInputsError(ValueError):
    """A ``hlens.p11.inputs@1.0.0`` record (or the params it goes into) is not valid."""


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def _json_copy(value: Any, what: str) -> Any:
    """A JSON-ready deep copy of ``value`` (canonical JSON round trip), or ``RunInputsError``."""
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RunInputsError(f"{what} is not canonical JSON: {exc}") from exc


def execution_payload(
    step: timedelta, warmup: timedelta, initial_equity: Decimal
) -> dict[str, Any]:
    """The ``execution`` block for a decision grid and an initial equity (what the authority
    compares, as canonical JSON, with the recorded block)."""
    if not isinstance(step, timedelta) or step <= timedelta(0):
        raise RunInputsError("decision_step must be a positive timedelta")
    if not isinstance(warmup, timedelta) or warmup < timedelta(0):
        raise RunInputsError("decision_warmup must be a non-negative timedelta")
    if step % _MICROSECOND or warmup % _MICROSECOND:
        raise RunInputsError("the decision grid must be whole microseconds")
    if (
        not isinstance(initial_equity, Decimal)
        or not initial_equity.is_finite()
        or initial_equity <= 0
    ):
        raise RunInputsError("initial_equity must be a positive finite Decimal")
    return {
        "decision_step_microseconds": step // _MICROSECOND,
        "decision_warmup_microseconds": warmup // _MICROSECOND,
        "initial_equity": str(initial_equity),
    }


def state_labeller_identity(
    *,
    state_spec: Any,
    state_provider: Any,
    feature_spec: Any,
    feature_provider: Any,
) -> dict[str, Any]:
    """The identity of the research loop's state labeller: the ``StateSpec`` and ``FeatureSpec``
    (ref + content hash) and their providers (``name@version`` + descriptor hash) whose causal P2
    labels the validator reads at each decision time, a missing label being ``"unknown"``."""
    state, feature = state_provider.descriptor, feature_provider.descriptor
    return {
        "format": STATE_LABELLER_FORMAT,
        "state_spec": str(state_spec.ref),
        "state_spec_hash": state_spec.content_hash(),
        "state_provider": f"{state.name}@{state.version}",
        "state_provider_hash": state.content_hash(),
        "feature_spec": str(feature_spec.ref),
        "feature_spec_hash": feature_spec.content_hash(),
        "feature_provider": f"{feature.name}@{feature.version}",
        "feature_provider_hash": feature.content_hash(),
        "missing_label": _MISSING_LABEL,
    }


@dataclass(frozen=True)
class RunInputs:
    """The inputs a new run records (module docs). Every field is required; ``None`` is an
    explicit "not used", never "unknown"."""

    decision_step: timedelta
    decision_warmup: timedelta
    initial_equity: Decimal
    family_trial_count: int
    validation_seed: int
    control_seeds: tuple[int, ...] | None
    cscv_partitions: int | None
    impact_coefficient: float | None
    state_labeller: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        execution_payload(self.decision_step, self.decision_warmup, self.initial_equity)
        if not _is_int(self.family_trial_count) or self.family_trial_count < 1:
            raise RunInputsError("family_trial_count must be an int >= 1")
        if not _is_int(self.validation_seed):
            raise RunInputsError("validation_seed must be an int")
        seeds = self.control_seeds
        if seeds is not None and (
            not isinstance(seeds, tuple) or not seeds or not all(_is_int(s) for s in seeds)
        ):
            raise RunInputsError("control_seeds must be None or a non-empty tuple of ints")
        if self.cscv_partitions is not None and not _is_int(self.cscv_partitions):
            raise RunInputsError("cscv_partitions must be None or an int")
        if self.impact_coefficient is not None and not _is_number(self.impact_coefficient):
            raise RunInputsError("impact_coefficient must be None or a number")
        labeller = self.state_labeller
        if labeller is not None:
            if not isinstance(labeller, Mapping) or not labeller:
                raise RunInputsError("state_labeller must be None or a non-empty JSON object")
            object.__setattr__(self, "state_labeller", _json_copy(dict(labeller), "state_labeller"))

    def execution_payload(self) -> dict[str, Any]:
        return execution_payload(self.decision_step, self.decision_warmup, self.initial_equity)

    def validation_payload(self) -> dict[str, Any]:
        return {
            "family_trial_count": self.family_trial_count,
            "validation_seed": self.validation_seed,
            "control_seeds": None if self.control_seeds is None else list(self.control_seeds),
            "cscv_partitions": self.cscv_partitions,
            "impact_coefficient": self.impact_coefficient,
            "state_labeller": None if self.state_labeller is None else dict(self.state_labeller),
        }

    def payload(self) -> dict[str, Any]:
        return {
            "format": RUN_INPUTS_FORMAT,
            "execution": self.execution_payload(),
            "validation": self.validation_payload(),
        }

    def text(self) -> str:
        """The recorded value: the canonical JSON text of ``payload()``."""
        try:
            return canonical_json(self.payload())
        except (TypeError, ValueError) as exc:
            raise RunInputsError(f"the run inputs are not canonical JSON: {exc}") from exc

    @classmethod
    def from_payload(cls, payload: Any) -> RunInputs:
        """Strict inverse of ``payload()`` (exact key sets, exact JSON types)."""
        if not isinstance(payload, dict) or set(payload) != {"format", "execution", "validation"}:
            raise RunInputsError("not a hlens.p11.inputs payload")
        if payload["format"] != RUN_INPUTS_FORMAT:
            raise RunInputsError(f"unknown run inputs format {payload['format']!r}")
        execution, validation = payload["execution"], payload["validation"]
        if not isinstance(execution, dict) or set(execution) != _EXECUTION_KEYS:
            raise RunInputsError("the execution block does not have exactly its fields")
        if not isinstance(validation, dict) or set(validation) != _VALIDATION_KEYS:
            raise RunInputsError("the validation block does not have exactly its fields")
        step, warmup = (
            execution["decision_step_microseconds"],
            execution["decision_warmup_microseconds"],
        )
        equity = execution["initial_equity"]
        if not _is_int(step) or not _is_int(warmup) or not isinstance(equity, str):
            raise RunInputsError("the execution block has values of the wrong type")
        try:
            parsed = Decimal(equity)
        except InvalidOperation as exc:
            raise RunInputsError(f"initial_equity {equity!r} is not a Decimal") from exc
        if str(parsed) != equity:
            raise RunInputsError(f"initial_equity {equity!r} is not in its canonical text")
        seeds = validation["control_seeds"]
        if seeds is not None and not isinstance(seeds, list):
            raise RunInputsError("control_seeds must be null or a list")
        return cls(
            decision_step=timedelta(microseconds=step),
            decision_warmup=timedelta(microseconds=warmup),
            initial_equity=parsed,
            family_trial_count=validation["family_trial_count"],
            validation_seed=validation["validation_seed"],
            control_seeds=None if seeds is None else tuple(seeds),
            cscv_partitions=validation["cscv_partitions"],
            impact_coefficient=validation["impact_coefficient"],
            state_labeller=validation["state_labeller"],
        )


def with_run_inputs(params: Mapping[str, RunParam], inputs: RunInputs) -> dict[str, RunParam]:
    """``params`` plus the record under ``RUN_INPUTS_KEY`` (refused when the key is taken)."""
    if RUN_INPUTS_KEY in params:
        raise RunInputsError(f"{RUN_INPUTS_KEY} is reserved for the run inputs record")
    if not isinstance(inputs, RunInputs):
        raise RunInputsError("inputs must be RunInputs")
    return {**dict(params), RUN_INPUTS_KEY: inputs.text()}


def strategy_params(params: Mapping[str, RunParam]) -> dict[str, RunParam]:
    """The strategy's own parameters: ``params`` without the reserved records (run inputs and
    the ADR-0103 P7 plan record)."""
    return {key: value for key, value in params.items() if key not in RESERVED_PARAM_KEYS}


def recorded_run_inputs(params: Mapping[str, RunParam]) -> RunInputs | None:
    """The record a run carries in ``repro.params`` (``None``: the run records none — an old run,
    never backfilled). A value that is not exactly the canonical text of a valid payload raises
    ``RunInputsError``."""
    if RUN_INPUTS_KEY not in params:
        return None
    text = params[RUN_INPUTS_KEY]
    if not isinstance(text, str):
        raise RunInputsError(f"{RUN_INPUTS_KEY} is not a JSON text")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RunInputsError(f"{RUN_INPUTS_KEY} is not JSON: {exc}") from exc
    inputs = RunInputs.from_payload(payload)
    if inputs.text() != text:
        raise RunInputsError(f"{RUN_INPUTS_KEY} is not in its canonical JSON form")
    return inputs
