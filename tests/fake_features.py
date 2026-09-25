"""证明 FeatureProvider contract suite 有效的替身（Phase 1 F4；ADR-0030 §4）。

两个**刻意不同**的合规替身：

- `LatestValueProvider`：逐评估时刻调用 `FeatureRequest.visible_at`，返回按 `event_time` 最后一条
  可见观察的 `x`（`Decimal`）；需要至少 `min_inputs` 条可见观察，否则 `None`；
- `RunningCountProvider`：不用 `visible_at`，自己按 `available_time` 单调推进指针、按键覆盖，
  返回可见观察 `n`（`int`）之和；少于 `min_inputs` 条时 `None`。

以及只带一处故障的变体（每个都继承一个合规替身，只改一个行为）。它们都不是 FeatureProvider 实现。
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from core.contracts.feature import (
    FeatureObservation,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import FeatureSpec

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=2)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "fake-features"})


def fake_spec(name: str, *, lag: timedelta = LAG, min_inputs: int = 2) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        version="1.0.0",
        definition=f"test double {name}",
        inputs=(Ref(kind=Kind.REPRESENTATION, name="test_points", version="1.0.0"),),
        params=FrozenMapping({"min_inputs": min_inputs}),
        available_lag=lag,
    )


def lineage(revision: str) -> SelectedRevisionLineage:
    return SelectedRevisionLineage(
        canonical_table="canonical.test_points",
        canonical_revision_id=revision,
        raw_table="raw.test_points",
        raw_revision_id=f"raw-{revision}",
        source_table="raw.test_sources",
        source_revision_id="src-1",
    )


def observation(
    key: str, minute: int, *, available_after: int, x: str, n: int, revision: str | None = None
) -> FeatureObservation:
    """Point ``key`` at ``T0 + minute``, available ``available_after`` minutes after it."""
    event = T0 + minute * MINUTE
    values: dict[str, Decimal | int | bool | str] = {"x": Decimal(x), "n": n, "tag": "point"}
    return FeatureObservation(
        observation_key=key,
        event_time=event,
        available_time=event + available_after * MINUTE,
        knowledge_time=event + (available_after + 1) * MINUTE,
        values=FrozenMapping(values),
        lineage=lineage(revision or f"rev-{key}"),
    )


#: Keys k0..k5 with uneven availability; k2 has a later replacement revision.
OBSERVATIONS = (
    observation("k0", 0, available_after=1, x="1.5", n=1),
    observation("k1", 1, available_after=1, x="2.25", n=2),
    observation("k2", 2, available_after=1, x="3.125", n=3),
    observation("k3", 3, available_after=4, x="-4.5", n=5),
    observation("k2", 2, available_after=6, x="3.5", n=30, revision="rev-k2-b"),
    observation("k4", 4, available_after=1, x="5.0625", n=7),
    observation("k5", 5, available_after=3, x="0.5", n=11),
)
EVALUATION_TIMES = tuple(T0 + minute * MINUTE for minute in range(0, 14))


def perturb(item: FeatureObservation) -> FeatureObservation:
    values = dict(item.values)
    values["x"] = values["x"] * 2 + 1  # type: ignore[operator]
    values["n"] = values["n"] + 7  # type: ignore[operator]
    return item.model_copy(update={"values": values})


class _Base:
    NAME: ClassVar[str]

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        self._specs = {str(spec.ref): spec for spec in specs}
        self._descriptor = ProviderDescriptor(
            name=self.NAME,
            version="1.0.0",
            deterministic=True,
            supported_features=FrozenMapping(
                {key: spec.content_hash() for key, spec in self._specs.items()}
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _spec(self, request: FeatureRequest) -> FeatureSpec:
        spec = self._specs.get(str(request.feature))
        if spec is None or spec.content_hash() != request.spec_hash:
            raise UnsupportedFeature(f"{request.feature} is not supported with this spec hash")
        return spec

    def compute(self, request: FeatureRequest) -> FeatureResult:
        spec = self._spec(request)
        values = self._values(request, spec)
        return FeatureResult.build(request, self.descriptor, values)

    def _values(self, request: FeatureRequest, spec: FeatureSpec) -> list[FeatureValue]:
        raise NotImplementedError


def _min_inputs(spec: FeatureSpec) -> int:
    value = spec.params["min_inputs"]
    assert isinstance(value, int)
    return value


class LatestValueProvider(_Base):
    NAME = "latest_value"

    def _source(
        self, request: FeatureRequest, at: datetime, lag: timedelta
    ) -> tuple[FeatureObservation, ...]:
        """The observations the value is taken from (compliant: the visible set)."""
        return request.visible_at(at, lag)

    def _values(self, request: FeatureRequest, spec: FeatureSpec) -> list[FeatureValue]:
        out = []
        for at in request.evaluation_times:
            source = self._source(request, at, spec.available_lag)
            if len(source) < _min_inputs(spec):
                out.append(self._none(at))
                continue
            last = max(source, key=lambda item: item.event_time)
            # The reported input is always taken from the honest visible set, so a faulty
            # ``_source`` is caught by what the value does, not by the bookkeeping.
            visible = request.visible_at(at, spec.available_lag)
            honest = max(visible, key=lambda item: item.event_time) if visible else None
            out.append(
                FeatureValue(
                    evaluation_time=at,
                    value=self._decorate(last.values["x"], at),
                    inputs_used=0 if honest is None else 1,
                    latest_input_available_time=None if honest is None else honest.available_time,
                )
            )
        return out

    def _none(self, at: datetime) -> FeatureValue:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)

    def _decorate(self, value: Any, at: datetime) -> Any:
        return value


class RunningCountProvider(_Base):
    NAME = "running_count"

    def _values(self, request: FeatureRequest, spec: FeatureSpec) -> list[FeatureValue]:
        lag = spec.available_lag
        latest: dict[str, FeatureObservation] = {}
        cursor = 0
        out = []
        for at in request.evaluation_times:
            while (
                cursor < len(request.observations)
                and request.observations[cursor].available_time + lag <= at
            ):
                item = request.observations[cursor]
                latest[item.observation_key] = item
                cursor += 1
            if len(latest) < _min_inputs(spec):
                out.append(FeatureValue(evaluation_time=at, value=None, inputs_used=0))
                continue
            total = sum(int(item.values["n"]) for item in latest.values())
            out.append(
                FeatureValue(
                    evaluation_time=at,
                    value=total,
                    inputs_used=len(latest),
                    latest_input_available_time=max(
                        item.available_time for item in latest.values()
                    ),
                )
            )
        return out


# ======================================================================================
# 单点故障变体
# ======================================================================================


class NondeterministicProvider(LatestValueProvider):
    """每次调用给值加一个递增的微小量。"""

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        super().__init__(specs)
        self._calls = itertools.count()

    def compute(self, request: FeatureRequest) -> FeatureResult:
        self._bump = next(self._calls)
        return super().compute(request)

    def _decorate(self, value: Any, at: datetime) -> Any:
        return value + Decimal(self._bump) / Decimal(10**18)


class PeeksAheadProvider(LatestValueProvider):
    """取值时使用全部观察（不看 available_time）。"""

    def _source(
        self, request: FeatureRequest, at: datetime, lag: timedelta
    ) -> tuple[FeatureObservation, ...]:
        return request.observations


class IgnoresLagProvider(LatestValueProvider):
    """取值时只按 available_time <= t 过滤，忽略 available_lag。"""

    def _source(
        self, request: FeatureRequest, at: datetime, lag: timedelta
    ) -> tuple[FeatureObservation, ...]:
        return request.visible_at(at, timedelta(0))


class ZeroFillProvider(LatestValueProvider):
    """历史不足时填 0 而不是 None。"""

    def _none(self, at: datetime) -> FeatureValue:
        return FeatureValue(evaluation_time=at, value=Decimal(0), inputs_used=0)


class ContextDependentProvider(LatestValueProvider):
    """值依赖请求里评估时刻的个数（同一时刻单独请求与批量请求结果不同）。"""

    def _values(self, request: FeatureRequest, spec: FeatureSpec) -> list[FeatureValue]:
        out = super()._values(request, spec)
        extra = Decimal(len(request.evaluation_times) - 1)
        return [
            item.model_copy(update={"value": item.value + extra})
            if isinstance(item.value, Decimal)
            else item
            for item in out
        ]


class CachingProvider(RunningCountProvider):
    """按评估时刻缓存结果：同一组时刻的新请求拿到旧结果。"""

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        super().__init__(specs)
        self._cache: dict[tuple[datetime, ...], FeatureResult] = {}

    def compute(self, request: FeatureRequest) -> FeatureResult:
        key = request.evaluation_times
        if key not in self._cache:
            self._cache[key] = super().compute(request)
        return self._cache[key]


class ForgedHashProvider(RunningCountProvider):
    """绕过校验返回一个自报 result_hash 不符的结果。"""

    def compute(self, request: FeatureRequest) -> FeatureResult:
        honest = super().compute(request)
        return FeatureResult.model_construct(
            **{**honest.__dict__, "result_hash": content_hash({"forged": honest.result_hash})}
        )


class NanValueProvider(LatestValueProvider):
    """绕过校验把一个值写成 NaN。"""

    def compute(self, request: FeatureRequest) -> FeatureResult:
        honest = super().compute(request)
        values = list(honest.values)
        values[-1] = FeatureValue.model_construct(
            **{**values[-1].__dict__, "value": Decimal("NaN")}
        )
        return FeatureResult.model_construct(**{**honest.__dict__, "values": tuple(values)})


class DriftingDescriptorProvider(RunningCountProvider):
    """每次读取 descriptor 版本都变。"""

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        super().__init__(specs)
        self._reads = itertools.count()

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor.model_copy(update={"version": f"1.0.{next(self._reads)}"})

    def compute(self, request: FeatureRequest) -> FeatureResult:
        spec = self._spec(request)
        return FeatureResult.build(request, self.descriptor, self._values(request, spec))


class AcceptsAnySpecProvider(RunningCountProvider):
    """不核对请求的 feature / spec hash，总按第一份规格计算。"""

    def _spec(self, request: FeatureRequest) -> FeatureSpec:
        return next(iter(self._specs.values()))
