"""`LlmCall` 的最小完整登记（ADR-0016，D-18）。

覆盖 ADR-0016 验收矩阵 1 ~ 17：三项内容引用必填、`ContentBlobRef` 的字段边界、
`provider` / `model` 非空、`called_at` 显式必填且无默认值、不得出现自报验证布尔、
`ContentBlobRef` 的注册与 Schema 边界、v1 只读路径仍接受旧的三哈希形状，
以及含 `llm_calls` 的实验身份回归。

**本文件断言的是公开行为**：线载荷的接受 / 拒绝、导出的 JSON Schema 与运行时同源、
跨模型的身份稳定性。不断言实现细节（校验器名称、私有属性、内部数据结构）。

**不在本轮范围**（ADR-0016「明确不做」与「运行时延期义务」）：不打开任何 `uri`、
不取回任何内容、不校验内容是否真的哈希成 `sha256`、不校验 `media_type` / `byte_size`
是否与实际内容相符、不判断一次实验是否登记了**所有**发生过的 LLM 调用。
本文件不得被解读为"完整输入输出"要求已经满足。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import V1_MODEL_NAMES, read_v1
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import ContentBlobRef, Contract
from core.domain.research import LlmCall, ReproducibilityTuple
from tests import factories

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
#: ADR-0016 实现批次新增的 v1 只读覆盖向量（与 `tests/vectors/v1/` 的冻结快照集分开存放）。
COVERAGE_VECTOR_DIR = REPO / "tests" / "vectors" / "v1_coverage"

GOOD_HASH = "4" * 64
T0 = datetime(2024, 1, 1, tzinfo=UTC)

#: 非法的 `sha256`：大写、63 / 65 位、非十六进制、空串。
BAD_SHA256 = ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "   ", "sha256:" + "a" * 57)


def _llm_payload(**overrides: Any) -> dict[str, Any]:
    """一次合法调用的**线载荷**（JSON 形状），供逐项删改使用。"""
    payload: dict[str, Any] = json.loads(factories.llm_call().model_dump_json())
    payload.update(overrides)
    return payload


# ======================================================================================
# 矩阵 1、2：三项内容引用全部必填
# ======================================================================================


@pytest.mark.parametrize("field", ["prompt", "input", "output"])
def test_each_content_reference_is_required(field: str) -> None:
    """矩阵 1：缺任一项都被拒绝——允许缺失等于允许记录一次无法复核的调用。"""
    payload = _llm_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        LlmCall.model_validate(payload)
    assert LlmCall.model_fields[field].is_required()


def test_a_fully_bound_call_is_accepted() -> None:
    """矩阵 2：三项齐全且合法时接受，且三项各自保留自己的 uri 与哈希。"""
    call = factories.llm_call()
    assert isinstance(call.prompt, ContentBlobRef)
    assert (call.prompt.uri, call.input.uri, call.output.uri) == (
        "s3://bucket/llm/prompt",
        "s3://bucket/llm/input",
        "s3://bucket/llm/output",
    )
    assert len({call.prompt.sha256, call.input.sha256, call.output.sha256}) == 3
    assert call.called_at == T0


def test_content_references_are_the_only_home_of_the_hashes() -> None:
    """哈希没有消失，只是换了位置：从自由字符串移到 `ContentBlobRef.sha256`。"""
    assert set(LlmCall.model_fields) == {
        "schema_version",
        "provider",
        "model",
        "prompt",
        "input",
        "output",
        "called_at",
    }
    assert set(ContentBlobRef.model_fields) == {
        "schema_version",
        "uri",
        "sha256",
        "media_type",
        "byte_size",
    }


# ======================================================================================
# 矩阵 3：旧的三个哈希字段不再被接受
# ======================================================================================


@pytest.mark.parametrize("legacy_field", ["prompt_hash", "input_hash", "output_hash"])
def test_legacy_hash_fields_are_rejected_as_extras(legacy_field: str) -> None:
    """矩阵 3：`extra="forbid"` 让旧形状 fail closed，而不是被静默忽略。"""
    with pytest.raises(ValidationError):
        LlmCall.model_validate(_llm_payload(**{legacy_field: GOOD_HASH}))
    assert legacy_field not in LlmCall.model_fields


def test_the_whole_legacy_llm_call_shape_is_rejected() -> None:
    """整份 v1 形状的三哈希载荷不是 v2 `LlmCall` 的合法输入（它只能走 v1 只读入口）。"""
    with pytest.raises(ValidationError):
        LlmCall.model_validate(
            {
                "provider": "p",
                "model": "m",
                "prompt_hash": GOOD_HASH,
                "input_hash": GOOD_HASH,
                "output_hash": GOOD_HASH,
            }
        )


# ======================================================================================
# 矩阵 4、5、6、7：`ContentBlobRef` 的字段边界
# ======================================================================================


@pytest.mark.parametrize("bad_uri", ["", " ", "   ", "\t", "\n"])
def test_uri_rejects_empty_and_whitespace_only(bad_uri: str) -> None:
    """矩阵 4：空串与**纯空白**都按"非空"语义拒绝（去空白后长度为 0）。"""
    with pytest.raises(ValidationError):
        ContentBlobRef(uri=bad_uri, sha256=GOOD_HASH)


def test_uri_scheme_is_deliberately_not_frozen() -> None:
    """ADR-0016 §D-18.1：只约束非空；存储方案未决（D-01、D-02），不在此写死。"""
    for uri in ("s3://bucket/key", "file:///tmp/x", "https://example.invalid/x", "blob-1"):
        assert ContentBlobRef(uri=uri, sha256=GOOD_HASH).uri == uri


@pytest.mark.parametrize("bad", BAD_SHA256)
def test_sha256_must_be_lowercase_64_hex(bad: str) -> None:
    """矩阵 5：复用 ADR-0015 的 `ContentHash`——大写、长度错、非十六进制全部拒绝。"""
    with pytest.raises(ValidationError):
        ContentBlobRef(uri="s3://bucket/key", sha256=bad)


def test_sha256_accepts_a_well_formed_content_hash() -> None:
    assert ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH).sha256 == GOOD_HASH


@pytest.mark.parametrize("bad_media_type", ["", " ", "   "])
def test_media_type_rejects_empty_values_when_provided(bad_media_type: str) -> None:
    """矩阵 6：`media_type` 与 `uri` 用**同一条**非空规则，纯空白同样拒绝。"""
    with pytest.raises(ValidationError):
        ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH, media_type=bad_media_type)


def test_media_type_is_optional_and_defaults_to_none() -> None:
    assert ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH).media_type is None
    ref = ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH, media_type="application/json")
    assert ref.media_type == "application/json"


def test_byte_size_rejects_negative_values() -> None:
    """矩阵 7：负字节数不是"未知"，是错误。"""
    for bad in (-1, -1024):
        with pytest.raises(ValidationError):
            ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH, byte_size=bad)


def test_byte_size_accepts_zero_and_absence() -> None:
    """零字节内容是合法内容；不提供则是"未登记大小"，两者不同。"""
    assert ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH).byte_size is None
    assert ContentBlobRef(uri="s3://bucket/key", sha256=GOOD_HASH, byte_size=0).byte_size == 0


# ======================================================================================
# 矩阵 8：provider / model 非空
# ======================================================================================


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("bad", ["", " ", "   "])
def test_provider_and_model_reject_empty_values(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        LlmCall.model_validate(_llm_payload(**{field: bad}))


@pytest.mark.parametrize("field", ["provider", "model"])
def test_provider_and_model_have_no_frozen_vocabulary(field: str) -> None:
    """ADR-0016「明确不做」：不定义取值集合或命名规范，只要求非空。"""
    call = LlmCall.model_validate(_llm_payload(**{field: "anything-goes_1"}))
    assert getattr(call, field) == "anything-goes_1"
    node = LlmCall.model_json_schema(mode="serialization")["properties"][field]
    assert "enum" not in node and "pattern" not in node
    assert node["minLength"] == 1


# ======================================================================================
# 矩阵 9、10、11：`called_at` 显式必填、无默认值、必须带时区
# ======================================================================================


def test_called_at_rejects_naive_datetime() -> None:
    """矩阵 9：naive datetime 没有时点意义（`UtcDatetime`）。"""
    with pytest.raises(ValidationError):
        factories.llm_call(called_at=datetime(2024, 1, 1))  # noqa: DTZ001 - 刻意构造 naive
    with pytest.raises(ValidationError):
        LlmCall.model_validate(_llm_payload(called_at="2024-01-01T00:00:00"))


def test_called_at_accepts_aware_datetimes_and_normalizes_to_utc() -> None:
    """矩阵 10：合法 UTC aware 时间被接受；非 UTC 时区按既有规则归一到 UTC。"""
    call = LlmCall.model_validate(_llm_payload(called_at="2024-01-01T00:00:00Z"))
    assert call.called_at == T0
    shifted = LlmCall.model_validate(_llm_payload(called_at="2024-01-01T08:00:00+08:00"))
    assert shifted.called_at == T0


def test_called_at_is_required_and_has_no_default() -> None:
    """矩阵 11：没有默认值——否则构造 DTO 的时刻会冒充调用时刻（ADR-0016 §D-18.2）。"""
    payload = _llm_payload()
    del payload["called_at"]
    with pytest.raises(ValidationError):
        LlmCall.model_validate(payload)
    field = LlmCall.model_fields["called_at"]
    assert field.is_required()
    assert field.default_factory is None
    schema = LlmCall.model_json_schema(mode="serialization")
    assert "called_at" in schema["required"]
    assert "default" not in schema["properties"]["called_at"]


# ======================================================================================
# 矩阵 12：不得出现任何自报验证标志
# ======================================================================================


@pytest.mark.parametrize("model", [LlmCall, ContentBlobRef], ids=lambda m: m.__name__)
def test_no_self_reported_verification_flags(model: type[Contract]) -> None:
    """矩阵 12：契约层打不开 `uri`，因此不得出现 `verified` 这类布尔（ADR-0016 §D-18.3）。"""
    for name, field in model.model_fields.items():
        assert field.annotation is not bool, f"{model.__name__}.{name} 是自报布尔标志"
        assert field.annotation != (bool | None), f"{model.__name__}.{name} 是自报布尔标志"
    forbidden = ("verified", "validated", "checked", "retrievable", "integrity")
    for name in model.model_fields:
        assert not any(word in name for word in forbidden), f"{model.__name__}.{name} 是自报标志"


def test_contract_layer_does_not_retrieve_or_verify_content() -> None:
    """不实现取回与核验：没有任何相关方法，延期义务写在 ADR 与文档里。"""
    for attr in ("fetch", "retrieve", "verify", "verify_content", "open"):
        assert not hasattr(ContentBlobRef, attr)
        assert not hasattr(LlmCall, attr)
    adr = (REPO / "docs" / "adr" / "0016-llmcall-content-bindings.md").read_text(encoding="utf-8")
    assert "运行时延期义务" in adr


# ======================================================================================
# 矩阵 13、14：`ContentBlobRef` 的注册与 Schema 边界
# ======================================================================================


def test_content_blob_ref_is_registered_and_exported(tmp_path: Path) -> None:
    """矩阵 13：新契约模型必须在注册表内，并导出独立的 current Schema。"""
    assert ContentBlobRef in CONTRACT_MODELS
    written = export_json_schemas(tmp_path)
    assert "ContentBlobRef" in written
    committed = CURRENT_SCHEMA_DIR / "ContentBlobRef.schema.json"
    assert committed.is_file()
    assert json.loads(committed.read_text(encoding="utf-8")) == json.loads(
        written["ContentBlobRef"].read_text(encoding="utf-8")
    )


def test_content_blob_ref_is_not_a_v1_model() -> None:
    """矩阵 14：它是 v2 才出现的模型，不进入 v1 只读清单，`schemas/v1/` 不新增文件。"""
    assert "ContentBlobRef" not in V1_MODEL_NAMES
    assert not (LEGACY_SCHEMA_DIR / "ContentBlobRef.schema.json").exists()
    names = {p.stem.removesuffix(".schema") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")}
    assert names == set(V1_MODEL_NAMES)
    assert len(names) == 35


def test_current_schema_count_grew_to_38(tmp_path: Path) -> None:
    """ADR-0016 后 current 模型数 37 → 38，Phase 1 B1 后 → 46，B2 后 → 59；
    全量导出与已提交内容逐文件一致。"""
    written = export_json_schemas(tmp_path)
    assert len(CONTRACT_MODELS) == 59
    assert len(written) == len(CONTRACT_MODELS)
    committed = {path.name for path in CURRENT_SCHEMA_DIR.glob("*.schema.json")}
    assert committed == {path.name for path in written.values()}
    for name, path in written.items():
        actual = json.loads((CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))
        assert actual == json.loads(path.read_text(encoding="utf-8")), f"{name} 的 Schema 已过期"


def test_schema_and_runtime_constraints_come_from_one_source() -> None:
    """导出的 Schema 必须与运行时约束同源，只读消费者不得看到更宽的契约。"""
    blob = ContentBlobRef.model_json_schema(mode="serialization")
    assert blob["properties"]["uri"]["minLength"] == 1
    assert blob["properties"]["sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert blob["required"] == ["uri", "sha256"]
    assert {"minLength": 1, "type": "string"} in blob["properties"]["media_type"]["anyOf"]
    assert {"minimum": 0, "type": "integer"} in blob["properties"]["byte_size"]["anyOf"]
    call = LlmCall.model_json_schema(mode="serialization")
    assert call["required"] == ["provider", "model", "prompt", "input", "output", "called_at"]
    for field in ("prompt", "input", "output"):
        assert call["properties"][field]["$ref"].endswith("/ContentBlobRef")


@pytest.mark.parametrize("owner", ["ReproducibilityTuple", "ExperimentMetadata"])
def test_llm_calls_owners_expose_the_new_shape(owner: str) -> None:
    """两处 `llm_calls` 的 Schema 都随之更新，不留旧的三哈希定义。"""
    schema = json.loads((CURRENT_SCHEMA_DIR / f"{owner}.schema.json").read_text("utf-8"))
    llm_def = schema["$defs"]["LlmCall"]["properties"]
    assert set(llm_def) >= {"prompt", "input", "output", "called_at"}
    assert not {"prompt_hash", "input_hash", "output_hash"} & set(llm_def)
    assert "ContentBlobRef" in schema["$defs"]


# ======================================================================================
# 矩阵 15：v1 只读路径仍接受旧的三哈希形状
# ======================================================================================


def _coverage_vector() -> dict[str, Any]:
    record: dict[str, Any] = json.loads(
        (COVERAGE_VECTOR_DIR / "llm_call.json").read_text(encoding="utf-8")
    )
    return record


def test_v1_reader_still_accepts_the_three_hash_llm_call() -> None:
    """矩阵 15：current `LlmCall` 的变化**不倒灌**进 v1；旧哈希按 v1 语义保持不变。

    该向量是 ADR-0016 实现批次新增的兼容测试资产（见 `tests/vectors/v1_coverage/README.md`），
    不是历史上真实存在过的 v1 记录，也**不**赋予任何 v2 登记 / 晋升资格（ADR-0009 §7）。
    """
    record = _coverage_vector()
    legacy = read_v1(record["payload"], model=record["model"])
    assert legacy.model == "LlmCall"
    assert legacy.content_hash == record["legacy_content_hash"], "固定旧哈希必须保持不变"
    assert legacy.payload["prompt_hash"] == record["payload"]["prompt_hash"]
    assert legacy.payload["input_hash"] == record["payload"]["input_hash"]
    assert legacy.payload["output_hash"] == record["payload"]["output_hash"]


def test_v1_llm_call_payload_is_not_a_v2_model_input() -> None:
    """读得了 ≠ 能用：v1 载荷仍然不是 v2 `LlmCall` 的合法输入。"""
    with pytest.raises(ValidationError):
        LlmCall.model_validate(dict(_coverage_vector()["payload"]))


def test_v1_snapshot_for_llm_call_is_untouched() -> None:
    """v1 快照保留 v1 当时的字段名：旧身份不被新规则重写。"""
    schema = json.loads((LEGACY_SCHEMA_DIR / "LlmCall.schema.json").read_text("utf-8"))
    assert set(schema["required"]) == {
        "provider",
        "model",
        "prompt_hash",
        "input_hash",
        "output_hash",
    }
    assert "called_at" not in schema["properties"]


# ======================================================================================
# 矩阵 16：含 `llm_calls` 的实验身份回归
# ======================================================================================


def test_experiment_hash_survives_json_round_trip_with_llm_calls() -> None:
    """矩阵 16：JSON 往返后 `experiment_hash` **按位**一致。"""
    repro = factories.repro_tuple(llm_calls=(factories.llm_call(),))
    assert repro.llm_calls[0].prompt.sha256 == factories.HASH_A
    restored = ReproducibilityTuple.model_validate_json(repro.model_dump_json())
    assert restored == repro
    assert restored.experiment_hash == repro.experiment_hash
    assert restored.model_dump_json() == repro.model_dump_json()


def test_llm_calls_are_part_of_the_experiment_identity() -> None:
    """内容引用进入实验身份：换掉输出内容就是另一个实验元组。"""
    base = factories.repro_tuple(llm_calls=(factories.llm_call(),))
    other = factories.repro_tuple(
        llm_calls=(
            factories.llm_call(
                output=factories.content_blob_ref(uri="s3://bucket/llm/output", sha256="9" * 64)
            ),
        )
    )
    empty = factories.repro_tuple()
    hashes = {base.experiment_hash, other.experiment_hash, empty.experiment_hash}
    assert len(hashes) == 3


def test_experiment_metadata_round_trips_with_llm_calls() -> None:
    """`ExperimentMetadata.llm_calls` 是同一个 `LlmCall`，同样要通过往返回归。"""
    meta = factories.experiment_metadata(llm_calls=(factories.llm_call(),))
    restored = ExperimentMetadata.model_validate_json(meta.model_dump_json())
    assert restored == meta
    assert restored.content_hash() == meta.content_hash()
    assert restored.llm_calls[0].called_at == T0
    with pytest.raises(ValidationError):
        factories.experiment_metadata(llm_calls=({"provider": "p", "model": "m"},))


# ======================================================================================
# 矩阵 17：缺口说明必须继续存在
# ======================================================================================


def test_the_documented_gap_is_still_open() -> None:
    """矩阵 17：登记结构完整 ≠ "完整输入输出"已满足，缺口说明不得被改写为"已满足"。"""
    doc = (REPO / "docs" / "architecture" / "06-experiment.md").read_text(encoding="utf-8")
    assert "登记缺口" in doc, "06-experiment.md 必须保留 LLM 完整输入输出的缺口说明"
    assert "仍未完全满足" in doc, "不得把结构化登记描述为已满足完整输入输出"
    for obligation in ("存储层", "可取回", "sha256", "Runner"):
        assert obligation in doc, f"缺口说明必须点名延期义务：{obligation}"
    security = (REPO / "docs" / "architecture" / "09-security.md").read_text(encoding="utf-8")
    assert "ContentBlobRef" in security, "审计描述必须与新的登记结构一致"
    for obligation in ("可取回", "数据外发", "完整性覆盖"):
        assert obligation in security, f"09-security.md 必须保留延期义务：{obligation}"
    assert "未实现" in security
