"""Cross-sectional P7 transformation Providers: ``rank_cs`` / ``quantile_cs`` (ADR-0100 §2).

These execute the FeatureSpecs that ``research.hypotheses.typed_plan_lowering`` emits for
``transformation`` nodes with ``transform`` ``rank_cs`` / ``quantile_cs`` (plan format 1.3.0):

- ``P7RankCsProvider`` serves ``p7.transformation.rank_cs@1.0.0``;
- ``P7QuantileCsProvider`` serves ``p7.transformation.quantile_cs@1.0.0``.

A cross-sectional value is not a function of one instrument's history, so these providers do not
implement the single-series ``FeatureProvider`` Protocol. They reuse its descriptor
(``ProviderDescriptor``) and error types, with a request / result shape of their own:
``CrossSectionalRequest`` carries per-member source-feature values keyed by bar ``interval_end``,
and the result holds one value per (evaluation point, universe member).

Semantics (ADR-0100 §2), exactly and nothing else:

- **Population**: every member of the pinned universe snapshot (the ``ResearchDatasetManifest``
  bound by the spec's ``universe_manifest_hash``) at the bar time. The bar time is the bar's
  ``interval_end`` ``T``. For a point-simulation manifest, members are effective only at
  ``T == simulation_time``; for an interval-simulation manifest a member is effective when
  ``effective_from <= T < effective_until`` (the manifest's UTC half-open span). No stale or
  future membership is ever substituted.
- **Alignment**: each evaluation point names one ``interval_end``; only member values of exactly
  that bar enter the cross-section (no cross-bar mixing, no forward fill).
- **Visibility**: a member value enters the cross-section at evaluation time ``t`` only when
  ``available_time + available_lag <= t`` (``available_lag`` is the spec's). Several visible values
  for the same (member, bar) are PIT replacements: the one with the greatest ``available_time``
  is used. A value can never be available before its bar closes (``available_time >= T``).
- **Missing**: a member without a visible value, or whose visible value is missing (``None``), is
  excluded from the population and gets a missing output. Nothing is filled or interpolated.
- **Rank**: with ``n`` valid members, ``rank = (count_less + 0.5 * (count_equal - 1)) / (n - 1)``
  (ties: average rank; ``count_equal`` includes the member itself), in ``[0, 1]``; ``n < 2`` gives
  a missing value for every member of that bar. Computed exactly as the fraction
  ``(2 * count_less + count_equal - 1) / (2 * (n - 1))``; ``rank_cs`` emits it as a ``Decimal``
  rounded half-even to the spec's ``output_decimal_places``.
- **Quantile**: ``min(floor(rank * buckets), buckets - 1)`` with the spec's explicit ``buckets``,
  computed on the exact fraction (integer floor division, no rounding step).

Numbers are ``Decimal`` / ``int`` (``bool`` and ``float`` are refused). Membership keys are
``UniverseMember.episode.observation_key()``. A value for an episode that is not a member of the
pinned universe at all is an input error (fail closed): it signals a mis-wired request.

Honest boundary: whether the supplied source values really are the source FeatureSpec's values for
those members, and whether the manifest is the registered one, belongs to the executor / Registry
(as for every FeatureProvider). These providers are not registered in any allowlist here and do not
change ``TypedPlan.runnable`` or ``compile_plan``.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from itertools import pairwise
from typing import Any, ClassVar, Final

from core.contracts.feature import (
    FeatureInputError,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import SHA256_PATTERN, FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import DatasetRef, FeatureSpec, Zone

__all__ = [
    "QUANTILE_CS_DEFINITION",
    "RANK_CS_DEFINITION",
    "CrossSectionalEvaluation",
    "CrossSectionalRequest",
    "CrossSectionalResult",
    "CrossSectionalValue",
    "MemberBarValue",
    "P7QuantileCsProvider",
    "P7RankCsProvider",
]

RANK_CS_DEFINITION: Final = "p7.transformation.rank_cs@1.0.0"
QUANTILE_CS_DEFINITION: Final = "p7.transformation.quantile_cs@1.0.0"
_SEMANTIC_VERSION: Final = "1.0.0"
_UNIVERSE_PREFIX: Final = "research_dataset:"
_MIN_POPULATION: Final = 2
_MIN_BUCKETS: Final = 2
_HASH = re.compile(SHA256_PATTERN)
#: Division context for the emitted rank: 50 significant digits, half-even, then quantized to the
#: spec's decimal places. Platform independent (``decimal`` rounds correctly).
_DIVISION: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)

#: The exact parameter set each served spec must carry (``typed_plan_lowering``, ADR-0100 §2).
_COMMON_PARAMS: Final = frozenset(
    {
        "operator",
        "provider",
        "semantic_version",
        "population",
        "universe",
        "universe_manifest_hash",
        "universe_spec",
        "universe_spec_hash",
        "alignment",
        "missing",
        "min_population",
        "ties",
    }
)
_FIXED_PARAMS: Final[Mapping[str, str | int]] = {
    "semantic_version": _SEMANTIC_VERSION,
    "population": "universe_snapshot_members_at_bar",
    "alignment": "bar_interval_end",
    "missing": "exclude_from_population",
    "min_population": _MIN_POPULATION,
    "ties": "average",
}

SourceValue = Decimal | int


def _utc(value: object, what: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{what} must be a timezone-aware UTC datetime")
    return value


def _source_value(value: object, what: str) -> SourceValue | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise ValueError(f"{what} must be Decimal, int or None (bool / float are refused)")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{what} must be a finite Decimal")
    return value


def _wire(value: SourceValue | None) -> str | int | None:
    """Hash form of a value: Decimal as its exact text, int as int, missing as null."""
    if isinstance(value, Decimal):
        return str(value)
    return value


def _dataset_identity(ref: DatasetRef) -> tuple[Zone, str, str, datetime, datetime]:
    """Semantic identity of a ``DatasetRef`` (without the contract envelope version)."""
    return (ref.zone, ref.table, ref.snapshot_id, ref.time_range_start, ref.time_range_end)


def _hash(value: object, what: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(f"{what} must be a lowercase SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class MemberBarValue:
    """One member's source-feature value for one bar (``interval_end``), as it became visible."""

    member: str
    interval_end: datetime
    available_time: datetime
    knowledge_time: datetime
    value: SourceValue | None

    def __post_init__(self) -> None:
        if not isinstance(self.member, str) or not self.member:
            raise ValueError("member must be a non-empty episode observation key")
        _utc(self.interval_end, "interval_end")
        _utc(self.available_time, "available_time")
        _utc(self.knowledge_time, "knowledge_time")
        _source_value(self.value, "value")
        if self.available_time < self.interval_end:
            raise ValueError("a bar value cannot be available before its bar closes (interval_end)")

    def payload(self) -> dict[str, Any]:
        return {
            "member": self.member,
            "interval_end": self.interval_end.astimezone(UTC).isoformat(),
            "available_time": self.available_time.astimezone(UTC).isoformat(),
            "knowledge_time": self.knowledge_time.astimezone(UTC).isoformat(),
            "value": _wire(self.value),
        }

    def _order(self) -> tuple[datetime, str, datetime]:
        return (self.interval_end, self.member, self.available_time)


@dataclass(frozen=True, slots=True)
class CrossSectionalEvaluation:
    """One evaluation point: the time ``t`` and the bar ``interval_end`` it ranks (``<= t``)."""

    evaluation_time: datetime
    interval_end: datetime

    def __post_init__(self) -> None:
        _utc(self.evaluation_time, "evaluation_time")
        _utc(self.interval_end, "interval_end")
        if self.interval_end > self.evaluation_time:
            raise ValueError("interval_end must not be later than evaluation_time")

    def payload(self) -> dict[str, str]:
        return {
            "evaluation_time": self.evaluation_time.astimezone(UTC).isoformat(),
            "interval_end": self.interval_end.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CrossSectionalRequest:
    """A cross-sectional computation request.

    - ``feature`` / ``spec_hash``: the served ``rank_cs`` / ``quantile_cs`` FeatureSpec;
    - ``source``: the spec's source FeatureSpec ref (``inputs[0]``) the member values belong to;
    - ``universe_manifest_hash``: the pinned universe manifest (must equal the spec's);
    - ``evaluations``: non-empty, strictly ascending in both ``evaluation_time`` and
      ``interval_end``;
    - ``observations``: member bar values, stored in canonical order; a repeated
      (member, interval_end, available_time) is refused (the PIT replacement would be ambiguous);
      every ``knowledge_time <= knowledge_cutoff``.
    """

    feature: Ref
    spec_hash: str
    source: Ref
    universe_manifest_hash: str
    knowledge_cutoff: datetime
    evaluations: tuple[CrossSectionalEvaluation, ...]
    observations: tuple[MemberBarValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.feature, Ref) or self.feature.kind is not Kind.FEATURE:
            raise ValueError("feature must be a kind=feature Ref")
        if not isinstance(self.source, Ref) or self.source.kind is not Kind.FEATURE:
            raise ValueError("source must be a kind=feature Ref")
        _hash(self.spec_hash, "spec_hash")
        _hash(self.universe_manifest_hash, "universe_manifest_hash")
        _utc(self.knowledge_cutoff, "knowledge_cutoff")
        evaluations = tuple(self.evaluations)
        if not evaluations or any(
            type(item) is not CrossSectionalEvaluation for item in evaluations
        ):
            raise ValueError("evaluations must be a non-empty tuple of CrossSectionalEvaluation")
        for earlier, later in pairwise(evaluations):
            if (
                later.evaluation_time <= earlier.evaluation_time
                or later.interval_end <= earlier.interval_end
            ):
                raise ValueError("evaluations must be strictly ascending in time and interval_end")
        observations = tuple(self.observations)
        if any(type(item) is not MemberBarValue for item in observations):
            raise ValueError("observations must be MemberBarValue instances")
        ordered = tuple(sorted(observations, key=MemberBarValue._order))
        for earlier, later in pairwise(ordered):
            if earlier._order() == later._order():
                raise ValueError(
                    f"duplicate value for member {later.member!r} at "
                    f"{later.interval_end.isoformat()} available {later.available_time.isoformat()}"
                )
        for item in ordered:
            if item.knowledge_time > self.knowledge_cutoff:
                raise ValueError(f"value of {item.member!r} is known after the knowledge_cutoff")
        object.__setattr__(self, "evaluations", evaluations)
        object.__setattr__(self, "observations", ordered)

    def payload(self) -> dict[str, Any]:
        return {
            "feature": str(self.feature),
            "spec_hash": self.spec_hash,
            "source": str(self.source),
            "universe_manifest_hash": self.universe_manifest_hash,
            "knowledge_cutoff": self.knowledge_cutoff.astimezone(UTC).isoformat(),
            "evaluations": [item.payload() for item in self.evaluations],
            "observations": [item.payload() for item in self.observations],
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True, slots=True)
class CrossSectionalValue:
    """The value of one universe member at one evaluation point.

    ``population`` is the number of valid members ``n`` of that bar's cross-section;
    ``latest_input_available_time`` is the greatest ``available_time`` among the values used
    (``None`` exactly when ``value`` is missing).
    """

    evaluation_time: datetime
    interval_end: datetime
    member: str
    value: SourceValue | None
    population: int
    latest_input_available_time: datetime | None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.latest_input_available_time is None):
            raise ValueError("latest_input_available_time is set exactly when value is present")
        if self.latest_input_available_time is not None and (
            self.latest_input_available_time > self.evaluation_time
        ):
            raise ValueError("latest_input_available_time must not be later than evaluation_time")
        if type(self.population) is not int or self.population < 0:
            raise ValueError("population must be a non-negative int")

    def payload(self) -> dict[str, Any]:
        latest = self.latest_input_available_time
        return {
            "evaluation_time": self.evaluation_time.astimezone(UTC).isoformat(),
            "interval_end": self.interval_end.astimezone(UTC).isoformat(),
            "member": self.member,
            "value": _wire(self.value),
            "population": self.population,
            "latest_input_available_time": None
            if latest is None
            else latest.astimezone(UTC).isoformat(),
        }


def _result_hash(
    request_hash: str, provider: str, provider_hash: str, values: Iterable[CrossSectionalValue]
) -> str:
    return content_hash(
        {
            "request_hash": request_hash,
            "provider": provider,
            "provider_hash": provider_hash,
            "values": [item.payload() for item in values],
        }
    )


@dataclass(frozen=True, slots=True)
class CrossSectionalResult:
    """One ``compute`` result; ``result_hash`` is recomputed at construction (never self-reported).

    ``values`` are ordered by (evaluation_time, member); every universe member effective at an
    evaluation point's bar has exactly one value there.
    """

    request_hash: str
    provider: str
    provider_hash: str
    values: tuple[CrossSectionalValue, ...]
    result_hash: str

    def __post_init__(self) -> None:
        expected = _result_hash(self.request_hash, self.provider, self.provider_hash, self.values)
        if self.result_hash != expected:
            raise ValueError("result_hash does not match the result content")

    @classmethod
    def build(
        cls,
        request: CrossSectionalRequest,
        descriptor: ProviderDescriptor,
        values: Iterable[CrossSectionalValue],
    ) -> CrossSectionalResult:
        items = tuple(values)
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            values=items,
            result_hash=_result_hash(request_hash, descriptor.plugin_key, provider_hash, items),
        )


@dataclass(frozen=True, slots=True)
class _Served:
    spec: FeatureSpec
    source: Ref
    manifest: ResearchDatasetManifest
    manifest_hash: str
    #: Episode observation key -> effective spans ((None, None) for a point simulation).
    spans: Mapping[str, tuple[tuple[datetime | None, datetime | None], ...]]
    simulation_time: datetime | None
    buckets: int | None
    decimal_places: int | None

    def members_at(self, interval_end: datetime) -> tuple[str, ...]:
        """Universe members effective at the bar time ``interval_end`` (sorted, see module doc)."""
        if self.simulation_time is not None:
            if interval_end != self.simulation_time:
                return ()
            return tuple(sorted(self.spans))
        return tuple(
            sorted(
                key
                for key, spans in self.spans.items()
                if any(
                    start is not None and end is not None and start <= interval_end < end
                    for start, end in spans
                )
            )
        )


class _CrossSectionalProvider:
    """Shared plumbing: served specs bound to their pinned universe manifests, descriptor."""

    NAME: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"
    DEFINITION: ClassVar[str]
    OPERATOR: ClassVar[str]

    def __init__(
        self, specs: Iterable[FeatureSpec], universes: Iterable[ResearchDatasetManifest]
    ) -> None:
        manifests: dict[str, ResearchDatasetManifest] = {}
        for manifest in universes:
            if type(manifest) is not ResearchDatasetManifest:
                raise TypeError("universes must be ResearchDatasetManifest instances")
            manifests[manifest.content_hash()] = manifest
        served: dict[str, _Served] = {}
        for spec in specs:
            if not isinstance(spec, FeatureSpec):
                raise TypeError("specs must be FeatureSpec instances")
            try:
                entry = self._bind(spec, manifests)
            except ValueError as exc:
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec: {exc}") from None
            if str(spec.ref) in served:
                raise ValueError(f"{spec.ref} is declared twice")
            served[str(spec.ref)] = entry
        if not served:
            raise ValueError("a provider must serve at least one spec")
        self._served = served
        self._descriptor = ProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_features=FrozenMapping(
                {key: entry.spec.content_hash() for key, entry in served.items()}
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    # ------------------------------------------------------------------ binding

    def _bind(self, spec: FeatureSpec, manifests: Mapping[str, ResearchDatasetManifest]) -> _Served:
        if spec.definition != self.DEFINITION:
            raise ValueError(f"definition must be {self.DEFINITION}")
        if not spec.deterministic:
            raise ValueError("the spec must be deterministic")
        params = spec.params
        expected_keys = _COMMON_PARAMS | self._extra_keys()
        if set(params) != expected_keys:
            raise ValueError(f"params must be exactly {', '.join(sorted(expected_keys))}")
        fixed = {
            **_FIXED_PARAMS,
            "operator": self.OPERATOR,
            "provider": f"{self.NAME}@{self.VERSION}",
        }
        for key, value in fixed.items():
            actual = params[key]
            if type(actual) is not type(value) or actual != value:
                raise ValueError(f"params[{key!r}] must be {value!r}")
        manifest_hash = params["universe_manifest_hash"]
        if not isinstance(manifest_hash, str) or manifest_hash not in manifests:
            raise ValueError("universe_manifest_hash names no supplied ResearchDatasetManifest")
        manifest = manifests[manifest_hash]
        dataset = manifest.dataset
        if dataset.zone is not Zone.RESEARCH_DATASET:  # Defensive: the manifest enforces this.
            raise ValueError("the universe manifest's dataset must be zone=research_dataset")
        if params["universe"] != f"{_UNIVERSE_PREFIX}{dataset.table}@{dataset.snapshot_id}":
            raise ValueError("params['universe'] does not name the manifest's dataset")
        binding = manifest.universe_spec
        if params["universe_spec"] != f"{binding.name}@{binding.version}" or (
            params["universe_spec_hash"] != binding.spec_hash
        ):
            raise ValueError("params['universe_spec'] / ['universe_spec_hash'] differ from it")
        if len(spec.inputs) != 2:
            raise ValueError("inputs must be exactly (source feature, pinned universe dataset)")
        source, universe_input = spec.inputs
        if not isinstance(source, Ref) or source.kind is not Kind.FEATURE:
            raise ValueError("inputs[0] must be the source FeatureSpec ref")
        if not isinstance(universe_input, DatasetRef) or _dataset_identity(
            universe_input
        ) != _dataset_identity(dataset):
            raise ValueError("inputs[1] must be the pinned universe manifest's DatasetRef")
        pit = manifest.point_in_time
        spans: dict[str, list[tuple[datetime | None, datetime | None]]] = {}
        for member in manifest.members:
            spans.setdefault(member.episode.observation_key(), []).append(
                (member.effective_from, member.effective_until)
            )
        buckets, places = self._shape(params)
        return _Served(
            spec=spec,
            source=source,
            manifest=manifest,
            manifest_hash=manifest_hash,
            spans={key: tuple(items) for key, items in spans.items()},
            simulation_time=pit.simulation_time,
            buckets=buckets,
            decimal_places=places,
        )

    def _extra_keys(self) -> frozenset[str]:
        raise NotImplementedError

    def _shape(self, params: Mapping[str, object]) -> tuple[int | None, int | None]:
        """Per-provider parameters: ``(buckets, output_decimal_places)``."""
        raise NotImplementedError

    def _value(self, served: _Served, numerator: int, denominator: int) -> SourceValue:
        """The output from the exact rank fraction ``numerator / denominator``."""
        raise NotImplementedError

    # ------------------------------------------------------------------ compute

    def compute(self, request: CrossSectionalRequest) -> CrossSectionalResult:
        if type(request) is not CrossSectionalRequest:
            raise TypeError("request must be a CrossSectionalRequest")
        served = self._served.get(str(request.feature))
        if served is None or served.spec.content_hash() != request.spec_hash:
            raise UnsupportedFeature(f"{self.NAME} does not serve {request.feature} with this hash")
        if request.universe_manifest_hash != served.manifest_hash:
            raise FeatureInputError("the request's universe manifest differs from the spec's")
        if request.source != served.source:
            raise FeatureInputError(f"source must be {served.source} (the spec's inputs[0])")
        known = served.spans
        by_bar: dict[datetime, list[MemberBarValue]] = {}
        for item in request.observations:
            if item.member not in known:
                raise FeatureInputError(
                    f"{item.member!r} is not a member of the pinned universe "
                    f"{served.spec.params['universe']}"
                )
            by_bar.setdefault(item.interval_end, []).append(item)
        lag = served.spec.available_lag
        values: list[CrossSectionalValue] = []
        for point in request.evaluations:
            bar_values = by_bar.get(point.interval_end, [])
            values.extend(self._cross_section(served, point, bar_values, lag))
        return CrossSectionalResult.build(request, self._descriptor, values)

    def _cross_section(
        self,
        served: _Served,
        point: CrossSectionalEvaluation,
        bar_values: Iterable[MemberBarValue],
        lag: timedelta,
    ) -> list[CrossSectionalValue]:
        population = served.members_at(point.interval_end)
        members = set(population)
        latest: dict[str, MemberBarValue] = {}
        for item in bar_values:  # Canonical order: ascending available_time per member.
            if item.member in members and item.available_time + lag <= point.evaluation_time:
                latest[item.member] = item  # PIT replacement: the latest visible value wins.
        valid = {key: item for key, item in latest.items() if item.value is not None}
        n = len(valid)
        ordered = sorted(item.value for item in valid.values() if item.value is not None)
        used_at = max((item.available_time for item in valid.values()), default=None)
        result: list[CrossSectionalValue] = []
        for member in population:
            item = valid.get(member)
            value: SourceValue | None = None
            if item is not None and item.value is not None and n >= _MIN_POPULATION:
                less = bisect_left(ordered, item.value)
                equal = bisect_right(ordered, item.value) - less
                value = self._value(served, 2 * less + equal - 1, 2 * (n - 1))
            result.append(
                CrossSectionalValue(
                    evaluation_time=point.evaluation_time,
                    interval_end=point.interval_end,
                    member=member,
                    value=value,
                    population=n,
                    latest_input_available_time=None if value is None else used_at,
                )
            )
        return result


class P7RankCsProvider(_CrossSectionalProvider):
    """``p7.transformation.rank_cs@1.0.0``: average-tie percentile rank in ``[0, 1]``."""

    NAME: ClassVar[str] = "p7_transformation_rank_cs"
    DEFINITION: ClassVar[str] = RANK_CS_DEFINITION
    OPERATOR: ClassVar[str] = "rank_cs"

    def _extra_keys(self) -> frozenset[str]:
        return frozenset({"scale", "output_decimal_places", "rounding"})

    def _shape(self, params: Mapping[str, object]) -> tuple[int | None, int | None]:
        if params["scale"] != "unit_interval":
            raise ValueError("params['scale'] must be 'unit_interval'")
        if params["rounding"] != "half_even":
            raise ValueError("params['rounding'] must be 'half_even'")
        places = params["output_decimal_places"]
        if type(places) is not int or places < 0:
            raise ValueError("params['output_decimal_places'] must be a non-negative int")
        return None, places

    def _value(self, served: _Served, numerator: int, denominator: int) -> SourceValue:
        places = served.decimal_places
        if places is None:  # pragma: no cover - `_shape` binds it for every served rank_cs spec
            raise RuntimeError("rank_cs spec has no bound output_decimal_places")
        exact = _DIVISION.divide(Decimal(numerator), Decimal(denominator))
        return exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


class P7QuantileCsProvider(_CrossSectionalProvider):
    """``p7.transformation.quantile_cs@1.0.0``: ``min(floor(rank * buckets), buckets - 1)``."""

    NAME: ClassVar[str] = "p7_transformation_quantile_cs"
    DEFINITION: ClassVar[str] = QUANTILE_CS_DEFINITION
    OPERATOR: ClassVar[str] = "quantile_cs"

    def _extra_keys(self) -> frozenset[str]:
        return frozenset({"buckets"})

    def _shape(self, params: Mapping[str, object]) -> tuple[int | None, int | None]:
        buckets = params["buckets"]
        if type(buckets) is not int or buckets < _MIN_BUCKETS:
            raise ValueError(f"params['buckets'] must be an int >= {_MIN_BUCKETS}")
        return buckets, None

    def _value(self, served: _Served, numerator: int, denominator: int) -> SourceValue:
        buckets = served.buckets
        if buckets is None:  # pragma: no cover - `_shape` binds it for every quantile_cs spec
            raise RuntimeError("quantile_cs spec has no bound buckets")
        # floor(rank * buckets) on the exact fraction; rank == 1 maps to the top bucket.
        return min((numerator * buckets) // denominator, buckets - 1)
