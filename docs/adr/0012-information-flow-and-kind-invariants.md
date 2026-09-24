# ADR-0012: 信息流白名单与 kind 判别字段（D-23）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-24，Codex 依 Raphael 授权批准） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3） |
| 影响范围 | Contract / Data（信息流方向） |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0008](0008-contract-payload-immutability.md)、[ADR-0010](0010-contract-construction-and-canonical-versioning.md) |

## 背景

`03-data.md` §2 与 `02-domain.md` §2 冻结了信息流方向：Canonical → Feature → State / Event →
Research Dataset，Outcome 由 Canonical 计算且**永不回流**为 Feature / State / Event 的输入
（Constitution C-L2）。但契约层目前几乎不校验这条方向：

1. **`kind` 可以被覆盖。** 每个具体规格都写成 `kind: Kind = Kind.FEATURE` 这样的**带默认值的可变字段**
   （`core/domain/specs.py:104,123,134,150,167,188` 等），因此可以构造一个
   `kind = "outcome"` 的 `FeatureSpec`。判别字段一旦可被覆盖，其它所有按 `kind` 做的校验都可以被绕过。
2. **输入类型没有白名单。** `FeatureSpec.inputs`（`specs.py:106`）是 `tuple[Ref | DatasetRef, ...]`，
   不限制 `Ref.kind`、不限制 `DatasetRef.zone`，也不要求至少一个输入；
   `StateSpec.features`（`:124`）、`EventSpec.features / states`（`:136-137`）同样只看数量不看类型。
   于是 `outcome:*` 引用或 `zone = outcome` 的数据集可以直接成为 Feature / State / Event 的输入——
   这正是 C-L2 要禁止的泄漏形态。`StrategySpec` 只禁止了 `signals` 中的 Outcome（`:178-182`），
   `risk_policy` 的 kind 则未校验。
3. **少数数值不变量缺失。** `EventSpec.observable_lag`（`:138`）没有非负校验，
   而同类字段 `available_lag` 有（`:113-117`、`:90-94`）；`StateSpec.training_window`（`:127`）
   可以是零或负；`state_space`（`:125`）只要求非空，允许重复标签。

## 精确决定

### D-23.1 `kind` 是不可覆盖的字面量判别字段

每个具体的 `VersionedSpec` 子类的 `kind` 冻结为该类型的**字面量**：它只接受自身那一个取值，
不能由构造载荷覆盖为别的 `Kind`。覆盖尝试被拒绝，而不是被静默接受。

涉及 `RepresentationSpec`、`FeatureSpec`、`StateSpec`、`EventSpec`、`OutcomeSpec`、
`StrategySpec`、`RiskPolicy`、`KnowledgeItem`、`Hypothesis`、`ExperimentSpec`、
`StrategyArtifact`、`ValidationProfile`、`ProfileSelectionRule`。

`Ref.kind` 与 `VersionedSpec` 基类的 `kind` 保持为完整 `Kind` 枚举——引用当然要能指向各种类型。

### D-23.2 信息流白名单

| 字段 | 允许的 `Ref.kind` | 允许的 `DatasetRef.zone` | 数量 |
|---|---|---|---|
| `RepresentationSpec.inputs` | 不接受 `Ref`（保持现状：只接受 `DatasetRef`） | 保持现状，不在本轮收紧 | 至少 1（现状） |
| `FeatureSpec.inputs` | `representation`、`feature` | `canonical`、`feature`、`research_dataset` | 至少 1 |
| `StateSpec.features` | `feature` | 不接受 | 至少 1（现状） |
| `EventSpec.features` | `feature` | 不接受 | 现状（features 与 states 至少其一非空） |
| `EventSpec.states` | `state` | 不接受 | 同上 |
| `StrategySpec.signals` | `feature`、`state`、`event` | 不接受 | 至少 1（现状） |
| `StrategySpec.risk_policy` | `risk` | 不接受 | 可选（现状） |

由该白名单直接得到的不变量，必须在文档与测试中显式表述：
**Outcome 的 `Ref` 与 `zone = outcome` 的 `DatasetRef` 都不得进入 Feature / State / Event /
Strategy 的输入**（Constitution C-L2、`03-data.md` §2）。

**`lineage` 不在本轮收紧。** `VersionedSpec.lineage`（`core/domain/base.py:354`）是**溯源**，
表达"这个对象从哪里来"，不是计算输入。把 Outcome 从 lineage 中排除会让"这个 Feature 的
定义曾参考过某个 Outcome 的研究"无法被记录，且与计算图的语义混淆。本 ADR 明确保留现状。

### D-23.3 数值与标签不变量

| 字段 | 规则 |
|---|---|
| `EventSpec.observable_lag` | `>= 0`（负的可观测延迟即未来函数） |
| `StateSpec.training_window` | 若提供则必须 `> 0`（`None` 表示不适用，仍然允许） |
| `StateSpec.state_space` | 标签唯一，不得重复 |

## 明确不做

- **不发明 Phase 1 的迟到 / 修订数据语义。** 本 ADR 不引入 `revision`、`as_of`、`vintage`
  或任何 point-in-time 修订模型。`available_time` 的既有定义（`03-data.md` §4）不变。
- 不收紧 `lineage`（见 D-23.2 末段）。
- 不收紧 `RepresentationSpec.inputs` 的 zone 集合；Representation 与原始数据的关系留待
  Phase 1 用真实数据讨论。
- 不校验被引用对象是否真的存在、版本是否已登记、内容是否与 `content_hash` 一致。
- 不做传递依赖闭包的方向性检查（见下一节）。
- 不改变 `Kind` 枚举的取值集合。
- 不改变任何字段的可选 / 必填状态，`EventSpec` 的"至少一个输入"规则保持现状。

## 运行时延期义务

契约层只能校验**直接引用所声明的类型**，这是必要条件，不等于信息流已验证：

| 义务 | 说明 |
|---|---|
| 传递闭包的方向性 | `feature:a` 的上游是否间接依赖了某个 Outcome，契约层拿不到上游实例 |
| 引用与实例一致 | 被引用的 `feature:x@1.0.0` 是否真的是 Feature、内容是否匹配，属 Registry |
| 物化数据的泄漏检测 | 实际数据是否使用了 `available_time > t` 的行，属 Runner 与验证服务的泄漏门（G1） |
| Research Dataset 的 point-in-time 对齐 | `zone = research_dataset` 的输入内部是否正确对齐，属 Phase 1+ 的数据层 |

**不得**把 D-23.2 的白名单表述为"泄漏已被防住"；它防的是**声明层面**的错误方向。

## Schema 与迁移影响

- 受影响模型：`RepresentationSpec`、`FeatureSpec`、`StateSpec`、`EventSpec`、`OutcomeSpec`、
  `StrategySpec`、`RiskPolicy`，以及所有 `kind` 被冻结为字面量的模型。
- `kind` 在 JSON Schema 中由枚举收窄为单值（`const` / 单元素 `enum`），这是外部消费者可见的收紧。
- 需要重新导出 `schemas/`；实际差异以实施时的重导出结果为准。
- `schemas/v1/` 与 `tests/vectors/v1/` **逐字节不变**。
- 不新增契约模型，`V1_MODEL_NAMES` 不变。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
也没有任何 v2 数据登记。因此本 ADR 是对同一个未发布版本的收窄，
`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本 ADR 已获批准；下列矩阵是**实现批次**的验收条件。实现尚未发生，矩阵未运行。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | 构造 `FeatureSpec(kind=Kind.OUTCOME, ...)`，以及其余每个具体规格的同类覆盖 | 全部拒绝 |
| 2 | 不传 `kind` 时构造各规格 | 接受，取自身字面量 |
| 3 | `FeatureSpec.inputs` 含 `outcome:*` 的 `Ref` | 拒绝 |
| 4 | `FeatureSpec.inputs` 含 `zone = outcome` 的 `DatasetRef` | 拒绝 |
| 5 | `FeatureSpec.inputs` 含 `zone = raw` / `state` / `event` 的 `DatasetRef` | 拒绝 |
| 6 | `FeatureSpec.inputs` 含 `representation` / `feature` 的 `Ref`，或 `canonical` / `feature` / `research_dataset` 的 `DatasetRef` | 接受 |
| 7 | `FeatureSpec.inputs` 为空 | 拒绝 |
| 8 | `StateSpec.features` 含非 `feature` 引用 | 拒绝 |
| 9 | `EventSpec.features` 含非 `feature`；`EventSpec.states` 含非 `state` | 均拒绝 |
| 10 | `StrategySpec.signals` 含 `outcome` / `risk` / `dataset` 引用 | 均拒绝 |
| 11 | `StrategySpec.risk_policy` 指向非 `risk` | 拒绝 |
| 12 | `EventSpec.observable_lag` 为负；为零 | 拒绝；接受 |
| 13 | `StateSpec.training_window` 为零 / 负；为 `None` | 前两者拒绝；`None` 接受 |
| 14 | `StateSpec.state_space` 含重复标签 | 拒绝 |
| 15 | `lineage` 中含 `outcome:*` 引用 | **接受**（溯源不是计算输入） |
| 16 | 导出的 Schema 中各具体规格的 `kind` | 为单值，不再是完整枚举 |
| 17 | `schemas/v1/` 35 份快照与 v1 固定向量 | 逐字节不变，旧哈希不变 |

## 后果

- 正面：C-L2 与 `03-data.md` §2 的信息流方向第一次在契约层有可执行反例；判别字段不能被伪造，
  因此所有基于 `kind` 的既有校验（ADR-0009 的引用 kind 检查等）不再能被绕过。
- 负面 / 代价：构造 Feature / State / Event 的调用方必须使用正确类型的引用，测试工厂需同步；
  `kind` 收窄为单值后，外部消费者若曾依赖"可传任意 kind"的宽松行为会被拒绝（仓库内无此用法）。
- 对复现性的影响：v1 只读路径与旧哈希不受影响；仓库内无历史规格数据受影响。

## 合规检查

- [ ] 不修改 Domain Contract 的既有方向（只把已冻结方向落成校验），变更经本 ADR 提出
- [ ] 不修改 Validation Constitution 与 roadmap
- [ ] 不修改任何已批准 ADR 的正文
- [ ] Domain 层仍只依赖标准库与 Pydantic
- [ ] 未实现 Registry / Runner / 泄漏检测；未引入自报布尔标志
