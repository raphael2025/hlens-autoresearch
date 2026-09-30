"""P7 feature operator execution Providers (ADR-0082 §2 / §4, ADR-0099, ADR-0100 item 1).

Each Provider serves exactly the ``FeatureSpec`` values emitted by the pure P7 lowering
(``research.hypotheses.typed_plan_lowering``) for one definition, and nothing else:

- ``P7StandardizeProvider`` (``p7_transformation_standardize@1.0.0``):
  ``p7.transformation.standardize@1.0.0``;
- ``P7DifferenceProvider`` (``p7_transformation_difference@1.0.0``):
  ``p7.transformation.difference@1.0.0``;
- ``P7SmoothSmaProvider`` (``p7_transformation_smooth@1.0.0``):
  ``p7.transformation.smooth_sma@1.0.0``;
- ``P7RankTsProvider`` (``p7_transformation_rank@1.0.0``): ``p7.transformation.rank_ts@1.0.0``;
- ``P7QuantileTsProvider`` (``p7_transformation_quantile@1.0.0``):
  ``p7.transformation.quantile_ts@1.0.0``;
- ``P7InteractionProductProvider`` (``p7_interaction_product@1.0.0``):
  ``p7.interaction.product@1.0.0``.

The Provider key of each class equals the ``provider`` value the lowering writes into the spec's
``params``; a spec whose params, lag, inputs or lineage differ from the lowered form is refused at
construction (fail closed).

**Inputs.** A P7 feature is computed from other features, not from raw observations directly. The
caller injects an explicit table ``str(input ref)`` -> ``UpstreamFeature(spec, provider)`` (no
registry lookup). At an evaluation time the Provider evaluates each upstream feature itself, with a
structurally truncated sub-request (``available_time + upstream.available_lag <= tau``, the same
truncation ``infrastructure.feature.runner`` applies, cut from the set visible at ``t``), and
checks every sub-result with ``FeatureResult.check_answers``. It therefore only ever uses data
with ``available_time <= t``.

**Bar grid (transformations).** ``window`` counts bars. At evaluation time ``t`` the Provider
orders the visible bars (observations with ``event_end_time``; one symbol; no overlap) by
``event_time`` and takes the trailing run of ``window`` bars (``window + 1`` for ``difference``).
The run must be contiguous (``end == next start``), otherwise the value is ``None``. The upstream
value "as of bar i" is the upstream feature evaluated at ``tau_i``, the latest ``available_time``
among the run's bars ``0..i`` (so ``tau_i <= t`` and ``tau`` never decreases). Backward-only: no
bar after ``t`` and no value computed with data later than ``tau_i`` is ever used.

**Missing values propagate.** A ``None`` upstream value anywhere in the window, too little or
non-contiguous history, or an undefined statistic (zero dispersion for ``standardize``) gives
``None``. Nothing is filled, interpolated or carried forward. A non-numeric upstream value
(``bool`` or text) is refused (``FeatureInputError``).

**Formulas** (``x_0 .. x_{n-1}`` the window's upstream values, ``x_{n-1}`` the current bar):

- ``standardize``: ``(x_{n-1} - mean) / sd`` over the ``window`` values, ``sd`` the population
  standard deviation (divide by ``window``, as ``zscore_reversion``); the rolling window *is* the
  fit window (Constitution C-L3), there is no full-sample fit;
- ``difference``: ``x_n - x_0`` over ``window + 1`` values (the ``window``-bar lag difference),
  exact;
- ``smooth_sma``: the simple moving average ``sum / window``;
- ``rank_ts``: ``(count_less + 0.5 * (count_equal - 1)) / (window - 1)`` of the current value
  among the ``window`` values including it (ties average; in ``[0, 1]``);
- ``quantile_ts``: ``floor(rank * buckets)`` clipped to ``[0, buckets - 1]`` (an ``int``);
- ``product``: ``a * b`` of the two inputs evaluated at the same evaluation time ``t`` (exact
  alignment by construction; either side ``None`` gives ``None``). The product must be exact:
  a result that cannot be represented exactly is refused, never rounded.

Division and square roots run in a fixed 50-significant-digit, half-even ``Context`` (the same
precision ``plugins.features.bars`` uses); ``difference`` and ``product`` are exact. Deterministic,
float-free, no clock and no randomness.

**Provenance.** ``inputs_used`` is the sum of the upstream values' own ``inputs_used``, capped at
the size of the visible set (upstream evaluations share observations).
``latest_input_available_time`` is the latest input time any upstream value reported. Every
upstream sub-request is cut from the visible set at ``t`` itself (``request.visible_at(t, 0)``),
never from the raw request, so a value at ``t`` is a function of that visible set only
(ADR-0030 §1) and a direct call and a call through ``infrastructure.feature.runner`` (which hands
over only that set) give identical answers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, localcontext
from itertools import pairwise
from typing import ClassVar, Final

from core.contracts.feature import (
    FeatureInputError,
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import FeatureSpec

__all__ = [
    "P7_FEATURE_PROVIDERS",
    "P7DifferenceProvider",
    "P7InteractionProductProvider",
    "P7QuantileTsProvider",
    "P7RankTsProvider",
    "P7SmoothSmaProvider",
    "P7StandardizeProvider",
    "UpstreamFeature",
]

_SEMANTIC_VERSION: Final = "1.0.0"
#: Same precision and rounding as ``plugins.features.bars`` (division / sqrt only).
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class UpstreamFeature:
    """One entry of the caller's upstream table: an input feature spec and its Provider."""

    spec: FeatureSpec
    provider: FeatureProvider


def _numeric(value: object, where: str) -> Decimal | None:
    """An upstream value as ``Decimal``; ``None`` stays missing; ``bool`` / text are refused."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise FeatureInputError(f"{where}: upstream value {value!r} is not a Decimal or int")
    return Decimal(value)


def _exact(operation: str, left: Decimal, right: Decimal) -> Decimal:
    """``left - right`` or ``left * right`` exactly; an inexact result is refused."""
    if operation == "product":
        digits = len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2
    else:
        exponents = (int(left.as_tuple().exponent), int(right.as_tuple().exponent))
        digits = max(left.adjusted(), right.adjusted()) - min(exponents) + 3
    context = Context(prec=max(digits, 28), rounding=ROUND_HALF_EVEN, traps=[Inexact])
    try:
        if operation == "product":
            return context.multiply(left, right)
        return context.subtract(left, right)
    except Inexact as exc:
        raise FeatureInputError(f"{operation} is not exactly representable") from exc


def _check_upstream(table: Mapping[str, UpstreamFeature]) -> dict[str, UpstreamFeature]:
    out: dict[str, UpstreamFeature] = {}
    for key, entry in table.items():
        if not isinstance(entry, UpstreamFeature) or not isinstance(entry.spec, FeatureSpec):
            raise ValueError(f"upstream entry {key!r} must be an UpstreamFeature")
        if key != str(entry.spec.ref):
            raise ValueError(f"upstream key {key!r} does not name its spec {entry.spec.ref}")
        if not entry.provider.descriptor.supports(entry.spec.ref, entry.spec.content_hash()):
            raise ValueError(f"the provider given for {key} does not support that spec's hash")
        out[key] = entry
    return out


class _Evaluation:
    """Upstream evaluations at one evaluation time ``t``, cached per (upstream, tau).

    ``visible`` is the visible set at ``t``; every sub-request is cut from it (module docstring).
    """

    __slots__ = ("_cache", "_request", "_visible")

    def __init__(self, request: FeatureRequest, visible: Sequence[FeatureObservation]) -> None:
        self._request = request
        self._visible = tuple(visible)
        self._cache: dict[tuple[str, datetime], FeatureValue] = {}

    def value(self, upstream: UpstreamFeature, tau: datetime) -> FeatureValue:
        key = (str(upstream.spec.ref), tau)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        lag = upstream.spec.available_lag
        sub = FeatureRequest(
            feature=upstream.spec.ref,
            spec_hash=upstream.spec.content_hash(),
            manifest_content_hash=self._request.manifest_content_hash,
            knowledge_cutoff=self._request.knowledge_cutoff,
            evaluation_times=(tau,),
            observations=tuple(
                item for item in self._visible if item.available_time + lag <= tau
            ),
        )
        result = upstream.provider.compute(sub)
        if not isinstance(result, FeatureResult):
            raise FeatureInputError(f"{upstream.spec.ref} did not return a FeatureResult")
        try:
            result.check_answers(sub, upstream.provider.descriptor, lag)
        except ValueError as exc:
            raise FeatureInputError(f"{upstream.spec.ref} answered inconsistently: {exc}") from exc
        (answer,) = result.values
        self._cache[key] = answer
        return answer


def _answer(
    at: datetime,
    value: Decimal | int | None,
    used: Sequence[FeatureValue],
    visible: Sequence[FeatureObservation],
) -> FeatureValue:
    """Build one output value with honest provenance (module docstring, **Provenance**)."""
    if value is None:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)
    reported = [item.latest_input_available_time for item in used]
    if any(time is None for time in reported) or not visible:
        raise FeatureInputError("an upstream value without inputs cannot feed a P7 value")
    latest = max(time for time in reported if time is not None)
    if latest not in {item.available_time for item in visible}:
        raise FeatureInputError(
            f"{at.isoformat()}: upstream input time {latest.isoformat()} is not a visible "
            "observation's available_time"
        )
    inputs = min(sum(item.inputs_used for item in used), len(visible))
    return FeatureValue(
        evaluation_time=at,
        value=value,
        inputs_used=max(inputs, 1),
        latest_input_available_time=latest,
    )


class _Bar:
    __slots__ = ("available_time", "end", "start")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time


def _bars(visible: Sequence[FeatureObservation]) -> list[_Bar]:
    symbols = {item.values.get("symbol") for item in visible}
    if len(symbols) > 1:
        raise FeatureInputError("a request must hold the bars of one symbol")
    bars = sorted((_Bar(item) for item in visible), key=lambda bar: bar.start)
    for earlier, later in pairwise(bars):
        if later.start < earlier.end:
            raise FeatureInputError(f"two bars overlap at {later.start.isoformat()}")
    return bars


def _trailing_grid(bars: Sequence[_Bar], count: int) -> list[datetime] | None:
    """``tau_0 .. tau_{count-1}`` of the contiguous trailing run, or ``None``."""
    if len(bars) < count:
        return None
    run = bars[len(bars) - count :]
    if any(earlier.end != later.start for earlier, later in pairwise(run)):
        return None
    grid: list[datetime] = []
    latest: datetime | None = None
    for bar in run:
        latest = bar.available_time if latest is None else max(latest, bar.available_time)
        grid.append(latest)
    return grid


def _positive_int(params: Mapping[str, object], name: str, minimum: int = 1) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")
    return value


class _P7FeatureProvider:
    """Shared plumbing: lowered-spec check, descriptor, upstream table."""

    NAME: ClassVar[str]
    VERSION: ClassVar[str] = _SEMANTIC_VERSION
    DEFINITION: ClassVar[str]
    ARITY: ClassVar[int]

    def __init__(
        self, specs: Iterable[FeatureSpec], *, upstream: Mapping[str, UpstreamFeature]
    ) -> None:
        table = _check_upstream(upstream)
        by_ref: dict[str, FeatureSpec] = {}
        for spec in specs:
            if not isinstance(spec, FeatureSpec):
                raise TypeError("specs must be FeatureSpec instances")
            self._check_spec(spec, table)
            if str(spec.ref) in by_ref:
                raise ValueError(f"{spec.ref} is declared twice")
            by_ref[str(spec.ref)] = spec
        if not by_ref:
            raise ValueError("a provider must serve at least one spec")
        self._specs = by_ref
        self._upstream = table
        self._descriptor = ProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_features=FrozenMapping(
                {key: spec.content_hash() for key, spec in by_ref.items()}
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @classmethod
    def plugin_key(cls) -> str:
        return f"{cls.NAME}@{cls.VERSION}"

    def _check_spec(self, spec: FeatureSpec, table: Mapping[str, UpstreamFeature]) -> None:
        where = f"{spec.ref} is not a {self.NAME} spec"
        if spec.definition != self.DEFINITION:
            raise ValueError(f"{where}: definition {spec.definition!r}")
        if spec.available_lag != timedelta(0) or spec.deterministic is not True:
            raise ValueError(f"{where}: the lowered spec has available_lag 0 and is deterministic")
        if len(spec.inputs) != self.ARITY or not all(
            isinstance(ref, Ref) and ref.kind is Kind.FEATURE for ref in spec.inputs
        ):
            raise ValueError(f"{where}: needs exactly {self.ARITY} feature input(s)")
        if tuple(spec.lineage) != tuple(spec.inputs):
            raise ValueError(f"{where}: lineage must equal the inputs, in order")
        expected = self._expected_params(spec.params)
        if dict(spec.params) != expected or any(
            type(spec.params[key]) is not type(value) for key, value in expected.items()
        ):
            raise ValueError(f"{where}: params differ from the lowered declaration")
        for ref in spec.inputs:
            if str(ref) not in table:
                raise ValueError(f"{spec.ref}: input {ref} is not in the upstream table")

    def compute(self, request: FeatureRequest) -> FeatureResult:
        spec = self._specs.get(str(request.feature))
        if spec is None or spec.content_hash() != request.spec_hash:
            raise UnsupportedFeature(f"{self.NAME} does not serve {request.feature} with this hash")
        inputs = tuple(self._upstream[str(ref)] for ref in spec.inputs)
        values = []
        for at in request.evaluation_times:
            visible = request.visible_at(at, spec.available_lag)
            evaluation = _Evaluation(request, visible)
            values.append(self._value(spec, at, inputs, evaluation, visible))
        return FeatureResult.build(request, self._descriptor, values)

    # ------------------------------------------------------------------ per provider

    def _expected_params(self, params: Mapping[str, object]) -> dict[str, object]:
        raise NotImplementedError

    def _value(
        self,
        spec: FeatureSpec,
        at: datetime,
        inputs: Sequence[UpstreamFeature],
        evaluation: _Evaluation,
        visible: Sequence[FeatureObservation],
    ) -> FeatureValue:
        raise NotImplementedError


class _TransformationProvider(_P7FeatureProvider):
    """A time-series transformation over the trailing bar grid (module docstring)."""

    ARITY: ClassVar[int] = 1
    TRANSFORM: ClassVar[str]
    MIN_WINDOW: ClassVar[int] = 1
    #: Extra bars the formula needs beyond ``window`` (``difference`` needs one).
    EXTRA_BARS: ClassVar[int] = 0

    def _extra_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {}

    def _expected_params(self, params: Mapping[str, object]) -> dict[str, object]:
        window = _positive_int(params, "window", self.MIN_WINDOW)
        return {
            "operator": self.TRANSFORM,
            "provider": f"p7_transformation_{self.TRANSFORM}@1.0.0",
            "semantic_version": _SEMANTIC_VERSION,
            "window": window,
            "direction": "backward_only",
            "missing": "propagate_none",
            **self._extra_params(params),
        }

    def _value(
        self,
        spec: FeatureSpec,
        at: datetime,
        inputs: Sequence[UpstreamFeature],
        evaluation: _Evaluation,
        visible: Sequence[FeatureObservation],
    ) -> FeatureValue:
        window = _positive_int(spec.params, "window", self.MIN_WINDOW)
        grid = _trailing_grid(_bars(visible), window + self.EXTRA_BARS)
        if grid is None:
            return _answer(at, None, (), visible)
        (upstream,) = inputs
        used = [evaluation.value(upstream, tau) for tau in grid]
        series: list[Decimal] = []
        for item in used:
            number = _numeric(item.value, f"{spec.ref} @ {at.isoformat()}")
            if number is None:
                return _answer(at, None, (), visible)
            series.append(number)
        return _answer(at, self._statistic(spec, series), used, visible)

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> Decimal | int | None:
        raise NotImplementedError


class P7StandardizeProvider(_TransformationProvider):
    """Rolling z-score of the current value; population sd; ``sd == 0`` -> ``None``."""

    NAME = "p7_transformation_standardize"
    DEFINITION = "p7.transformation.standardize@1.0.0"
    TRANSFORM = "standardize"

    def _extra_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {"fit_scope": "rolling_training_window"}

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> Decimal | None:
        with localcontext(_CONTEXT):
            count = Decimal(len(series))
            mean = sum(series, Decimal(0)) / count
            variance = sum(((value - mean) ** 2 for value in series), Decimal(0)) / count
            if variance == 0:
                return None
            return (series[-1] - mean) / variance.sqrt()


class P7DifferenceProvider(_TransformationProvider):
    """``x_t - x_{t - window bars}`` (exact), from ``window + 1`` contiguous bars."""

    NAME = "p7_transformation_difference"
    DEFINITION = "p7.transformation.difference@1.0.0"
    TRANSFORM = "difference"
    EXTRA_BARS = 1

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> Decimal:
        return _exact("difference", series[-1], series[0])


class P7SmoothSmaProvider(_TransformationProvider):
    """Simple moving average of the trailing ``window`` values."""

    NAME = "p7_transformation_smooth"
    DEFINITION = "p7.transformation.smooth_sma@1.0.0"
    TRANSFORM = "smooth"

    def _extra_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {"algorithm": "simple_moving_average"}

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> Decimal:
        with localcontext(_CONTEXT):
            return sum(series, Decimal(0)) / Decimal(len(series))


def _rank_counts(series: Sequence[Decimal]) -> tuple[int, int]:
    """``(2 * count_less + count_equal - 1, 2 * (window - 1))``: the rank as an exact ratio."""
    current = series[-1]
    less = sum(1 for value in series if value < current)
    equal = sum(1 for value in series if value == current)
    return 2 * less + equal - 1, 2 * (len(series) - 1)


class P7RankTsProvider(_TransformationProvider):
    """Time-series percentile rank of the current value in ``[0, 1]`` (ties average)."""

    NAME = "p7_transformation_rank"
    DEFINITION = "p7.transformation.rank_ts@1.0.0"
    TRANSFORM = "rank"
    MIN_WINDOW = 2

    def _extra_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {"ties": "average", "scale": "unit_interval"}

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> Decimal:
        numerator, denominator = _rank_counts(series)
        with localcontext(_CONTEXT):
            return Decimal(numerator) / Decimal(denominator)


class P7QuantileTsProvider(_TransformationProvider):
    """``floor(rank * buckets)`` clipped to ``[0, buckets - 1]``, computed exactly (``int``)."""

    NAME = "p7_transformation_quantile"
    DEFINITION = "p7.transformation.quantile_ts@1.0.0"
    TRANSFORM = "quantile"
    MIN_WINDOW = 2

    def _extra_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {"buckets": _positive_int(params, "buckets", 2), "ties": "average"}

    def _statistic(self, spec: FeatureSpec, series: Sequence[Decimal]) -> int:
        buckets = _positive_int(spec.params, "buckets", 2)
        numerator, denominator = _rank_counts(series)
        bucket = (numerator * buckets) // denominator
        return min(max(bucket, 0), buckets - 1)


class P7InteractionProductProvider(_P7FeatureProvider):
    """Exact product of two features at the same evaluation time (ADR-0082 §2 / §3)."""

    NAME = "p7_interaction_product"
    DEFINITION = "p7.interaction.product@1.0.0"
    ARITY = 2

    def _expected_params(self, params: Mapping[str, object]) -> dict[str, object]:
        return {
            "alignment": "exact_evaluation_time",
            "missing": "propagate_none",
            "numeric_domain": "decimal_or_int_excluding_bool",
            "operator": "product",
            "provider": "p7_interaction_product@1.0.0",
            "semantic_version": _SEMANTIC_VERSION,
        }

    def _value(
        self,
        spec: FeatureSpec,
        at: datetime,
        inputs: Sequence[UpstreamFeature],
        evaluation: _Evaluation,
        visible: Sequence[FeatureObservation],
    ) -> FeatureValue:
        used = [evaluation.value(upstream, at) for upstream in inputs]
        left, right = (_numeric(item.value, f"{spec.ref} @ {at.isoformat()}") for item in used)
        if left is None or right is None:
            return _answer(at, None, (), visible)
        return _answer(at, _exact("product", left, right), used, visible)


#: Every P7 feature Provider class, by the lowered definition it serves.
P7_FEATURE_PROVIDERS: Final[Mapping[str, type[_P7FeatureProvider]]] = {
    cls.DEFINITION: cls
    for cls in (
        P7StandardizeProvider,
        P7DifferenceProvider,
        P7SmoothSmaProvider,
        P7RankTsProvider,
        P7QuantileTsProvider,
        P7InteractionProductProvider,
    )
}
