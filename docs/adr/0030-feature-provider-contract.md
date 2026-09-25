# ADR-0030: FeatureProvider 的 Protocol、DTO 与 provider-agnostic 契约测试（F4 前置）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-25，待 Raphael / Codex 决定；批准前不改 `core/`） |
| 日期 | 2026-09-25 |
| 决策者 | 待定 |
| 起草者 | Claude Code（Opus），Phase 1 F4 前置设计（docs-only） |
| 相关 Phase | Phase 1（roadmap 验收 #19） |
| 影响范围 | Contract（`core/contracts/` 新增模块，additive） |
| 是否破坏兼容 | 否：只新增模型与 Protocol；已发布 2.0.0 模型、`FeatureSpec` 与 Schema 不变 |
| 前置 | [ADR-0017](0017-provider-delivery-schedule.md)（交付义务已接受）、[ADR-0023](0023-bitemporal-revision-data.md)、[ADR-0028](0028-dual-raw-canonical-lineage.md) |

## 背景

ADR-0017 已决定：FeatureProvider 的可执行 Protocol、DTO（带 `schema_version`、导出 JSON Schema）与 provider-agnostic
契约测试，必须在 Phase 1 首个 Feature 实现之前交付于 `core/contracts/`。roadmap #19 要求 Feature guard 只使用
`available_time <= simulation_time` 且 `knowledge_time <= knowledge_cutoff` 的输入，并且结果可复现。
交付义务已定，本 ADR 只提出**字段与语义**，因为它改动 `core/contracts/`（H1）。

## 裁决（提案）

### 1. 泄漏由执行器结构性保证，而不是信任 Provider

- Provider **看不到**未来：执行器（Phase 1 F4 的 feature runner）对每个评估时刻 `t` 只交给 Provider
  `available_time <= t` 且 `knowledge_time <= knowledge_cutoff` 的输入观察（截断，不是标注）。
- `FeatureSpec.available_lag`（= declared latency）由执行器施加：`t` 时刻可用的输入必须满足
  `available_time + available_lag <= t`；Provider 不自行处理 lag。
- 契约测试额外做**因果扰动**：改变 `t` 之后可用的输入，`t` 的值必须不变（防御执行器以外的调用方式）。

### 2. DTO（`core/contracts/feature.py`，新增）

| 模型 | 字段 | 不变量 |
|---|---|---|
| `FeatureObservation` | `observation_key`、`event_time`、`event_end_time?`、`available_time`、`knowledge_time`、`values: FrozenMapping[str, Decimal \| int \| bool \| str]`、`lineage: SelectedRevisionLineage` | UTC；`available_time >= event_end_time or event_time`；值域拒绝 NaN / ±Inf（ADR-0013） |
| `FeatureRequest` | `feature: Ref(kind=feature) + spec_hash`、`manifest_content_hash`、`knowledge_cutoff`、`evaluation_times`（升序、唯一、非空）、`observations`（按 `available_time`, `observation_key` 规范排序） | 每条观察 `knowledge_time <= knowledge_cutoff`；请求内容哈希稳定 |
| `FeatureValue` | `evaluation_time`、`value: Decimal \| int \| bool \| None`（`None` = 历史不足，显式"不可计算"，不填补）、`inputs_used: int`、`latest_input_available_time?` | `latest_input_available_time + available_lag <= evaluation_time` |
| `FeatureResult` | `request_hash`、`provider: name@version + content_hash`、`values`（与 `evaluation_times` 一一对应）、`result_hash` | 同一请求 + 同一 provider 版本 → 同一 `result_hash` |
| `ProviderDescriptor` | `name`、`version`（SemVer）、`deterministic: Literal[True]`、`supported_features` | FeatureProvider 必须确定性（05-plugin §3） |

### 3. Protocol（`core/contracts/feature.py`）

```python
class FeatureProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...
    def compute(self, request: FeatureRequest) -> FeatureResult: ...
```

### 4. provider-agnostic 契约测试（`tests/contract_suites/feature.py`）

确定性（同请求两次同 `result_hash`）；因果扰动（改 `t` 之后可用的观察，`t` 的值不变）；lag（在
`available_time + lag > t` 时该观察不得影响 `t`）；截止（`knowledge_time > knowledge_cutoff` 的观察被请求构造拒绝）；
不可计算显式为 `None`；值与时刻一一对应；拒绝 NaN / ±Inf；哈希随任一输入变化。
与 B3 相同，以两个刻意不同的替身证明 suite 接受合规实现，并以只带一处故障的变体证明它能抓住故障。

### 5. Representation

"基础 Representation"（时间 bar、成交量 bar）在 Phase 1 **不是** Provider：它们是版本化的确定性变换，
由 `RepresentationSpec` 绑定（例如 E4 的 `hlens.canonical.resample@1.0.0`），不设独立 Protocol；
若将来需要可插拔的 Representation，再按 ADR-0017 节奏另立 Provider。

## 备选方案

| 方案 | 内容 | 缺点 | 结论 |
|---|---|---|---|
| **A（建议）** | 执行器截断 + 契约扰动测试 | 需要执行器（F4 实现） | 采纳 |
| B | Provider 自行按 `available_time` 过滤 | 信任插件；一个错误插件即泄漏 | 拒绝（LLM / 研究代码不得自证） |
| C | 只做契约扰动测试、不截断 | 测试覆盖有限，运行时仍可泄漏 | 拒绝 |
| D | 同时为 Representation 立 Provider | Phase 1 无可插拔需求 | 延后 |

## 契约、Schema 与迁移影响

新增 5 个模型与 1 个 Protocol，导出 JSON Schema（current 74 → 79），不升 `CONTRACT_SCHEMA_VERSION`（与 B1～B3 同样
为新增模型）；`FeatureSpec` 与既有 Schema 逐字节不变；无数据迁移。

## 后果

- 正面：Feature 泄漏由结构保证；首批 FeatureProvider 可以任何实现通过同一套测试；复现以 `result_hash` 可检。
- 负面：执行器成为关键路径（F4 实现批次）；首批 Feature 需等 F3（Research Dataset）可构建才能在真实数据上运行，
  而 F3 依赖 E2（ADR-0029）。

## 合规检查

- [x] 不破坏已冻结契约（additive）
- [x] 不修改 Validation Constitution
- [x] Domain 层仍无具体技术依赖
- [x] Outcome 不进入 Feature 输入（`FeatureSpec` 输入白名单不变）
