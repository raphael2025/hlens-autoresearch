"""逐实例记忆的 `content_hash()`（G3-P2）：纯性能，哈希 / 相等 / 复制 / pickle 语义不变。

- 金样本：记忆后的哈希与改动前的算法（`content_hash(model_dump(mode="json", exclude=...))`）
  逐一相等，并与改动前的代码实际算出的十六进制值（`GOLDEN`，在 `ac2daef` 上生成）逐字相等；
- `FeatureRequest` 复用观察片段拼出的哈希输入与基类算法逐字节相同；
- 记忆不跨实例、不跨复制 / pickle 传播，不参与 `==`，不进入 JSON Schema；
- 任何换掉或改写顶层字段的路径（`model_copy(update=...)`、`model_validate`、`__init__` 重入、
  `__setstate__`、绕过 frozen 的 `object.__setattr__`）都不会留下过期的哈希。
"""

from __future__ import annotations

import copy
import inspect
import pickle
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.feature import FeatureObservation, FeatureRequest, FeatureResult
from core.domain.base import (
    Contract,
    FrozenMapping,
    Kind,
    Ref,
    canonical_json,
    content_hash,
    contract_schema_version_scope,
)
from core.domain.specs import FeatureSpec
from infrastructure.feature.runner import run_feature
from plugins.features import BarRealizedVolatilityProvider, BarVolumeSumProvider
from tests import factories, fake_features
from tests.contract_version_support import at_pre_bump
from tests.plugins.features.test_bar_features import CUTOFF, MANIFEST, MINUTE, SUITE_BARS, T0, bar

_MEMO_SLOTS = ("_content_hash_memo", "_canonical_dump_memo")


def _reference_hash(item: Contract) -> str:
    """改动前 `Contract.content_hash()` 的原样实现。"""
    return content_hash(item.model_dump(mode="json", exclude=item._non_semantic_fields()))


def _has_memo(item: Contract) -> bool:
    return any(_has_slot(item, name) for name in _MEMO_SLOTS)


def _has_slot(item: Contract, name: str) -> bool:
    try:
        Contract.__dict__[name].__get__(item)
    except AttributeError:
        return False
    return True


def _same(item: Any) -> Any:
    return item


def _bar_spec(inputs: Callable[[Any], Any] = _same) -> FeatureSpec:
    """The provider's spec, mapped by ``inputs`` (its input Ref is an import-time constant)."""
    spec: FeatureSpec = inputs(
        BarRealizedVolatilityProvider.spec(2, available_lag=timedelta(minutes=1))
    )
    return spec


def _bar_request(
    bars: tuple[FeatureObservation, ...],
    *times: datetime,
    inputs: Callable[[Any], Any] = _same,
) -> FeatureRequest:
    spec = _bar_spec(inputs)
    return FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=times,
        observations=bars,
    )


def _odd_observations() -> tuple[FeatureObservation, ...]:
    """`str` / `bool` / `int` / 指数形 `Decimal` / 非 ASCII / 无 `event_end_time` 的观察。"""
    event = T0 + 3 * MINUTE
    values: dict[str, Decimal | int | bool | str] = {
        "label": '行情 "quoted" \\ \n',
        "flag": True,
        "count": 0,
        "tiny": Decimal("1E-30"),
        "neg": Decimal("-0.000"),
    }
    return (
        FeatureObservation(
            observation_key="点:αβ:1",
            event_time=event,
            available_time=event,
            knowledge_time=event + MINUTE,
            values=FrozenMapping(values),
            lineage=fake_features.lineage("rev-odd"),
        ),
        fake_features.observation("k9", 1, available_after=0, x="7", n=-3),
    )


def _feature_samples(inputs: Callable[[Any], Any]) -> Iterator[tuple[str, Contract]]:
    spec = fake_features.fake_spec("latest_x")
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=fake_features.MANIFEST,
        knowledge_cutoff=fake_features.CUTOFF,
        evaluation_times=fake_features.EVALUATION_TIMES,
        observations=tuple(inputs(item) for item in fake_features.OBSERVATIONS),
    )
    yield "fake_spec", spec
    yield "fake_request", request
    provider = fake_features.LatestValueProvider((spec,))
    yield "fake_descriptor", provider.descriptor
    yield "fake_result", provider.compute(request)
    yield "fake_run", run_feature(provider, spec, request)
    times = tuple(T0 + minute * MINUTE for minute in range(0, 17))
    bars = _bar_request(tuple(inputs(item) for item in SUITE_BARS), *times, inputs=inputs)
    yield "bar_request", bars
    bar_spec = _bar_spec(inputs)
    yield "bar_run", run_feature(BarRealizedVolatilityProvider((bar_spec,)), bar_spec, bars)
    odd = _odd_observations()
    yield "odd_request", _bar_request(odd, T0 + 5 * MINUTE, inputs=inputs)
    yield "empty_request", _bar_request((), T0, inputs=inputs)
    volume = inputs(BarVolumeSumProvider.spec(3, available_lag=timedelta(minutes=1)))
    yield "volume_spec", volume


def _walk(name: str, item: Any) -> Iterator[tuple[str, Contract]]:
    """该契约及其全部嵌套契约（按字段路径命名）。"""
    if isinstance(item, Contract):
        yield name, item
        for field in type(item).model_fields:
            yield from _walk(f"{name}.{field}", getattr(item, field))
    elif isinstance(item, tuple):
        for index, element in enumerate(item):
            yield from _walk(f"{name}[{index}]", element)
    elif isinstance(item, FrozenMapping):
        for key in sorted(item):
            yield from _walk(f"{name}[{key!r}]", item[key])


def golden_sample(inputs: Callable[[Any], Any] = _same) -> list[tuple[str, Contract]]:
    """`tests/factories.py` 的全部无参构造器 + F4 特征 DTO，连同它们的全部嵌套契约。

    ``inputs`` maps the module-level input objects built at import time (the pinned values were
    taken at 2.0.0: ``at_pre_bump`` gives their 2.0.0 twins; ADR-0052 §4).
    """
    roots: list[tuple[str, Contract]] = []
    for name, factory in sorted(vars(factories).items()):
        if not inspect.isfunction(factory) or factory.__module__ != factories.__name__:
            continue
        signature = inspect.signature(factory)
        if any(
            param.default is inspect.Parameter.empty
            and param.kind not in (param.VAR_KEYWORD, param.VAR_POSITIONAL)
            for param in signature.parameters.values()
        ):
            continue
        made = factory()
        if isinstance(made, Contract):
            roots.append((name, inputs(made)))
    roots.extend(_feature_samples(inputs))
    sample: list[tuple[str, Contract]] = []
    for name, root in roots:
        sample.extend(_walk(name, root))
    return sample


#: 改动前（`ac2daef`）的代码对金样本根对象算出的内容哈希。`experiment_metadata` 不在其中：
#: 它的语义字段含构造时刻，每次运行都不同（仍由上面的逐一对照覆盖）。
GOLDEN: dict[str, str] = {
    "bar_request": "2d9d93eea5f9d60c9c8e408a915bada5483bab6ce458fa90819ff310f565c0d1",
    "bar_run": "c81a0e270dcd592ba122fd3155085bfffbf23b1f5d34d08f7e8cca2e53817cdb",
    "content_blob_ref": "d70eab65f472b030d5f90ca98be62b1156027d7ee37e99d95c8c93903ccd3e13",
    "cost_model_ref": "b42204feda899c20f39f821760fe87ab89bcdbdf631e2fe2acbaa1cab8a88c92",
    "dataset_ref": "07d3141ced990120ac6aa42a94ffb2418e895fe90b8df7ccaf69b47f09a4125d",
    "deployment_record": "784eb3541308497a38d54b5ab7ed4c5fae98f1a268ca6c6bb32374995d4cfa46",
    "empty_request": "e32a6bef36210825a262fca6c7f6056d626a8806cce0a65e2fe2d0ec2ad4ada3",
    "equivalence_check": "074084a7127bee68a048bc9f4148b27e0f076d525d59ca7c374f05e728127ca7",
    "experiment_run": "d4b0dddd4635f76a586f3567b420efc3e894bc8c1b81c0f3b790fed071f58e3d",
    "experiment_spec": "c57bb908d66c2dea1ac1538d14f3facc8a222ac942ac132c51f69824389dff32",
    "fake_descriptor": "6057a39c53e3ed7eaf1339c9fddf4dc2df2d827a6641b0e1ed9a6f6cea4f320b",
    "fake_request": "89f01526b00c22176c720b4c1bd19516db86a72e07d590af1f919e26d5f55ecf",
    "fake_result": "0f1cb97eba8979fa44e17260d9ef052e0933959c67126eee71b57e0c97d46956",
    "fake_run": "0f1cb97eba8979fa44e17260d9ef052e0933959c67126eee71b57e0c97d46956",
    "fake_spec": "83e51360a30cf7d80c2d1a00e25022546df4aa2c164171ab3ee72fa9f088de87",
    "frozen_profile": "4ccd172282cf13f0c44c2166cf326585d91cb8a91aad48b5afe7d768bf688baf",
    "gate_result": "9932e55ade5a2b91c22f7af10b0c1aa2a114c9c6a5e6c5433b2b885e90a94686",
    "git_code_revision": "c53bd68cbb91d09954a6aea24c95812e7bb4e985db20cc770301139d767deb37",
    "golden_outputs": "95112286eded17d0d03d8c37553c162cab77ddd29828c821ef23daa073b8bc2c",
    "hypothesis_ref": "ed0d2d5ce872c3ee15102f3888d6d61b55be8b596e663c86e0ed25980314fab8",
    "lifecycle_history": "88d497fa1652f0a489341642a290a39f7e86be8b60e5323ecb53bd106645d055",
    "llm_call": "935ee09a7b4d991aea629b0535542e720cdeebe62af60dbbe46cd4b38a98b740",
    "odd_request": "de70858db7764ea6eb624a10bcb6aecbeee04aed6441989f1f4ff0a884243935",
    "outcome_ref": "2264766fbbcd0bcb13790f2a227f7fc592d8a37d1056aa13f7d3dd9f43d9e98e",
    "profile_ref": "696f5d8941647835a24659707dba21666b642ab7a26d14a6763e54589d26e775",
    "profile_selection": "c25917f32215b56f58eb9321543287962bdf4a76303ce4e5337d1547c7ca9935",
    "repro_tuple": "a8686cc57f5f646a89ec8339d68afc67a3184889a88e648572e58e2197b00914",
    "risk_ref": "0da9d4ae7f900b2177819bcee7dc1bd6dea56680a7b7544cae115da316cd8c32",
    "selection_key": "ce2feb5fda68210b08c4c48975304d17b194a67268191516f105d3c41a037c08",
    "selection_rule": "767ab9a039be05cc82b6613705276cac4c30ed0bb467f5b4152c95ac6576c7cc",
    "selection_rule_ref": "ea7036a5df069d81c92278a25cac3881448ce2f964df1d547c4fbccdf4509adc",
    "strategy_artifact": "3a6438018eaf860299a0c2288bb5fa2b5329f5c0cf586f92209502c2a60cfe30",
    "strategy_ref": "e6f26965db1264f64a4eb58d4316c3d50e89267457658121c4cf4698238af060",
    "validation_profile": "135a7bc546249a314e6a3c64d2102a73eedad5ad45fd872ebe0f4398c827a99e",
    "validation_report": "173cc2cb8cdf659768c39a7130591ea610e619a6051786d60469be14ed078ae2",
    "volume_spec": "e6e2c1639bc157c5b5ce3af25870c86a58ad67c1e0140b1f993313284cd16c29",
}


# ======================================================================================
# 哈希不变
# ======================================================================================


def test_the_golden_sample_is_broad() -> None:
    sample = golden_sample()
    assert len(sample) > 150
    assert len({type(item) for _, item in sample}) > 25


def test_memoized_hashes_equal_the_previous_algorithm() -> None:
    for name, item in golden_sample():
        expected = _reference_hash(item)
        assert item.content_hash() == expected, name
        assert item.content_hash() == expected, name  # 第二次读记忆


def test_memoized_hashes_equal_the_values_pinned_before_the_change() -> None:
    # Pinned at 2.0.0: the same objects, built by the 2.0.0 code (every envelope 2.0.0),
    # hash as pinned (ADR-0052 §4).
    with contract_schema_version_scope("2.0.0"):
        sample = golden_sample(inputs=at_pre_bump)
    hashes = {name: item.content_hash() for name, item in sample}
    assert GOLDEN
    for name, expected in GOLDEN.items():
        assert hashes[name] == expected, name


def test_feature_request_fragments_are_byte_identical_to_the_base_algorithm() -> None:
    requests = [item for _, item in golden_sample() if type(item) is FeatureRequest]
    assert len(requests) >= 4
    for request in requests:
        full = canonical_json(
            request.model_dump(mode="json", exclude=request._non_semantic_fields())
        )
        assert request._semantic_canonical_json() == full
        assert Contract._semantic_canonical_json(request) == full
        for item in request.observations:
            assert item._canonical_dump_json() == canonical_json(item.model_dump(mode="json"))


def test_runner_sub_requests_hash_as_before() -> None:
    """执行器的每个子请求（同一批观察实例的前缀）都与基类算法一致；与改动前的值一致。

    改动前的值在 2.0.0 下生成：本测试在 2.0.0 构造作用域内复算（ADR-0052 §4）。
    """
    with contract_schema_version_scope("2.0.0"):
        _runner_sub_requests_hash_as_before()


def _runner_sub_requests_hash_as_before() -> None:
    bars = tuple(bar(i, str(100 + i % 7)) for i in range(40))
    times = tuple(T0 + (i + 1) * MINUTE for i in range(40))
    request = _bar_request(bars, *times, inputs=at_pre_bump)
    seen: list[FeatureRequest] = []

    class Spy(BarRealizedVolatilityProvider):
        def compute(self, request: FeatureRequest) -> FeatureResult:
            seen.append(request)
            return super().compute(request)

    spec = _bar_spec(at_pre_bump)
    result = run_feature(Spy((spec,)), spec, request)
    assert len(seen) == len(times)
    for sub in seen:
        assert sub.content_hash() == _reference_hash(sub)
    assert request.content_hash() == _reference_hash(request)
    assert (request.content_hash(), result.result_hash) == (
        GOLDEN_RUN_REQUEST_HASH,
        GOLDEN_RUN_RESULT_HASH,
    )


#: `test_runner_sub_requests_hash_as_before` 的请求 / 结果哈希，在改动前（`ac2daef`）生成。
GOLDEN_RUN_REQUEST_HASH = "6807c63fbcb01d369d900ecf64a9b4b15233917c6b3ab08fd1194e5d5ad1b46e"
GOLDEN_RUN_RESULT_HASH = "c5e0663ac234f0b6764654fd20f07ac1afe734cc4ef03ee869d979ab149f4a68"


# ======================================================================================
# 记忆不改变相等、复制、pickle 与 Schema
# ======================================================================================


def _cached_request() -> FeatureRequest:
    request = _bar_request(SUITE_BARS, T0 + 9 * MINUTE)
    request.content_hash()
    return request


def test_the_memo_is_not_part_of_equality_or_python_hash() -> None:
    fresh = _bar_request(SUITE_BARS, T0 + 9 * MINUTE)
    cached = _cached_request()
    assert _has_memo(cached)
    assert not _has_memo(fresh)
    assert cached == fresh
    assert fresh == cached
    ref = Ref(kind=Kind.FEATURE, name="x", version="1.0.0")
    before = hash(ref)
    ref.content_hash()
    assert hash(ref) == before == hash(Ref(kind=Kind.FEATURE, name="x", version="1.0.0"))
    assert "memo" not in repr(cached)


def test_the_memo_is_not_a_pydantic_field_private_attribute_or_schema_member() -> None:
    for model in (Contract, FeatureRequest, FeatureObservation, Ref):
        assert model.__private_attributes__ == {}
        assert not set(_MEMO_SLOTS) & set(model.model_fields)
    schema = canonical_json(FeatureRequest.model_json_schema(mode="serialization"))
    assert "memo" not in schema
    assert "memo" not in canonical_json(_cached_request().model_dump(mode="json"))


@pytest.mark.parametrize("protocol", range(2, pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_round_trip_starts_with_an_empty_memo(protocol: int) -> None:
    # 协议 0 / 1 本就不能 pickle `FrozenMapping`（带 `__slots__`，改动前即如此），见下一个用例。
    cached = _cached_request()
    loaded = pickle.loads(pickle.dumps(cached, protocol=protocol))
    assert loaded == cached
    assert not _has_memo(loaded)
    assert loaded.content_hash() == cached.content_hash() == _reference_hash(loaded)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_every_pickle_protocol_still_works_for_a_contract_without_mappings(protocol: int) -> None:
    ref = Ref(kind=Kind.FEATURE, name="x", version="1.0.0")
    digest = ref.content_hash()
    loaded = pickle.loads(pickle.dumps(ref, protocol=protocol))
    assert loaded == ref
    assert not _has_memo(loaded)
    assert loaded.content_hash() == digest


@pytest.mark.parametrize(
    "duplicate",
    [copy.copy, copy.deepcopy, lambda m: m.model_copy(), lambda m: m.model_copy(deep=True)],
    ids=["copy", "deepcopy", "model_copy", "model_copy_deep"],
)
def test_copies_start_with_an_empty_memo_and_hash_the_same(duplicate: Any) -> None:
    cached = _cached_request()
    duplicated = duplicate(cached)
    assert duplicated is not cached
    assert duplicated == cached
    assert not _has_memo(duplicated)
    assert duplicated.content_hash() == cached.content_hash()


def test_weak_references_still_work() -> None:
    import weakref

    request = _cached_request()
    assert weakref.ref(request)() is request


# ======================================================================================
# 没有过期的记忆
# ======================================================================================


def test_model_copy_update_produces_a_new_hash() -> None:
    cached = _cached_request()
    original = cached.content_hash()
    later = cached.model_copy(update={"evaluation_times": (T0 + 10 * MINUTE,)})
    assert later.content_hash() != original
    assert later.content_hash() == _reference_hash(later)
    assert cached.content_hash() == original == _reference_hash(cached)
    same = cached.model_copy(update={"evaluation_times": cached.evaluation_times})
    assert same.content_hash() == original


def test_model_copy_update_of_an_observation_changes_the_request_hash() -> None:
    request = _cached_request()
    first = request.observations[0]
    first._canonical_dump_json()
    changed = first.model_copy(update={"observation_key": "bar:BTC-USDT:0b"})
    assert changed._canonical_dump_json() != first._canonical_dump_json()
    other = request.model_copy(update={"observations": (changed, *request.observations[1:])})
    assert other.content_hash() != request.content_hash()
    assert other.content_hash() == _reference_hash(other)


def test_no_stale_memo_after_model_validate() -> None:
    cached = _cached_request()
    dumped = cached.model_dump(mode="json")
    again = FeatureRequest.model_validate_json(canonical_json(dumped))
    assert again.content_hash() == cached.content_hash()
    python_payload = cached.model_dump()
    python_payload["manifest_content_hash"] = "e" * 64
    changed = FeatureRequest.model_validate(python_payload)
    assert not _has_memo(changed)
    assert changed.content_hash() != cached.content_hash()
    assert changed.content_hash() == _reference_hash(changed)
    # 已是实例的输入按 Pydantic 默认原样返回：同一对象，记忆也就是它自己的。
    assert FeatureRequest.model_validate(cached) is cached


def test_re_running_init_on_an_instance_does_not_keep_the_old_hash() -> None:
    cached = _cached_request()
    before = cached.content_hash()
    payload = cached.model_dump()
    payload["manifest_content_hash"] = "e" * 64
    cached.__init__(**payload)  # type: ignore[misc]
    assert cached.manifest_content_hash == "e" * 64
    assert cached.content_hash() != before
    assert cached.content_hash() == _reference_hash(cached)


def test_setstate_on_a_live_instance_does_not_keep_the_old_hash() -> None:
    cached = _cached_request()
    other = cached.model_copy(update={"manifest_content_hash": "e" * 64})
    cached.__setstate__(other.__getstate__())
    assert cached.content_hash() == other.content_hash() == _reference_hash(other)


def test_bypassing_frozen_on_a_top_level_field_does_not_keep_the_old_hash() -> None:
    """诚实边界之外的低层改写（ADR-0008 决策 2），顶层字段仍然不会留下过期哈希。"""
    cached = _cached_request()
    before = cached.content_hash()
    object.__setattr__(cached, "manifest_content_hash", "e" * 64)
    assert cached.content_hash() != before
    assert cached.content_hash() == _reference_hash(cached)
    observation = cached.observations[0]
    dumped = observation._canonical_dump_json()
    observation_copy = observation.model_copy()
    cached.__dict__["knowledge_cutoff"] = CUTOFF + MINUTE
    assert cached.content_hash() == _reference_hash(cached)
    object.__setattr__(observation_copy, "observation_key", "moved")
    assert observation_copy._canonical_dump_json() != dumped
    assert observation_copy.content_hash() == _reference_hash(observation_copy)


def test_the_memo_is_per_instance() -> None:
    at = datetime(2024, 3, 1, 12, 9, tzinfo=UTC)
    a = _bar_request(SUITE_BARS, at)
    b = _bar_request(SUITE_BARS[:-1], at)
    assert a.content_hash() != b.content_hash()
    assert a.content_hash() == _reference_hash(a)
    assert b.content_hash() == _reference_hash(b)
