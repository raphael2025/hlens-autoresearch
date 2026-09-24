# ADR-0018: 契约值对象的语义身份（D-26）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24；待 Codex 复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 的项目技术决策授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 0（批次 C2） |
| 影响范围 | Contract / Validation（Profile 选择）/ Lifecycle |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见「版本」一节） |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0009](0009-experiment-identity-binding.md)、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md)、[ADR-0015](0015-audit-identity-types-and-version-bindings.md) |
| 来源 | [Phase 0 关闭复审 C1](../reviews/2026-09-24-phase0-closing-review-c1.md) F1（P1）、F3（P2） |

## 背景

每个契约模型都继承 `Contract.schema_version`（`core/domain/base.py:302`）。嵌套的值对象因此也各自带一个
"信封版本"，它参与 Pydantic 的结构相等（`==`）与 `model_dump_json()`，也参与内容哈希。
C1 复审用探针证实了两处后果：

1. **F1（P1）Profile 选择的唯一映射可被绕过。** `ProfileSelectionRule._unique_keys`
   （`core/contracts/profile_selection.py:65-69`）用 `entry.key.model_dump_json()` 判重，
   `select()`（`:71-81`）用 `entry.key == key` 匹配。两个业务字段完全相同、只差嵌套
   `schema_version`（`2.0.0` / `2.0.1`）的 key 被当成两条不同条目，分别映射到 `strict` 与
   `lenient` Profile，规则被接受；查询方只要改写信封版本就能选中不同 Profile。
   这违反 Constitution C-A4 与 ADR-0007 §3 规则 5（Profile 由确定性映射选定，研究者不得自选）。
   同时 `ProfileSelectionKey.research_class`（`core/domain/selection.py:23`）只要求非空，
   而同一概念的 `ProfileScope.research_class`（`core/contracts/validation_profile.py:75`）有标识符约束。
2. **F3（P2）跨对象的"是否指向同一目标"用了全结构相等。** 生命周期与 LIVE 证据的 subject 比较
   （`core/lifecycle/strategy.py:205,207,232,253`）用 `Ref` 的 `==`；两个 `str()` 完全相同、
   只差信封版本的 `Ref` 会被判为不同对象（fail closed，但语义错误）。

## 决策（D-26）

### D-26.1 `ProfileSelectionKey` 的选择键身份

- 选择键身份**精确**为 `(venue, symbol, timeframe, research_class)`。
- Contract 信封字段 `schema_version` **不参与**选择键身份。
- 实施批次提供**一个**语义身份函数（命名由实施批次确定，语义以本条为准）。

### D-26.2 重复检测与查询共用同一个身份函数

`ProfileSelectionRule` 的重复检测与 `select()` **必须**使用 D-26.1 的同一个身份函数，
不得一处用序列化文本、另一处用结构相等。因此：

- 同一业务键仅靠嵌套 `schema_version` 不同，在规则内判为**重复**并拒绝；
- 查询端使用同 major 的不同信封版本，匹配**同一条** entry；
- 任何业务字段不同，都不匹配。

### D-26.3 `research_class` 的标识符约束

`ProfileSelectionKey.research_class` 与 `ProfileScope.research_class` 使用**同一个**标识符约束
（当前为 `^[a-z][a-z0-9_]*$`），实施时应共用同一个常量，而不是两处各写一份。
取值集合仍未决定（D-09 H-7），本条不预设任何分类。

`ExperimentMetadata.declared_research_class` 已由既有校验要求等于 `profile_selection.key.research_class`，
因此被间接约束；本 ADR 不对它单独增加字段约束。

### D-26.4 `venue` / `symbol` / `timeframe` 是精确的不透明值

- 在本 Phase 内三者都是**区分大小写的精确不透明值**；**不做**隐式大小写折叠，也**不做** Unicode 规范化。
  因此 `binance` 与 `Binance` 仍是两个不同的键，可以作为两条条目共存——这是明确接受的边界，不是遗漏。
- 未来的 Provider / Adapter 必须产出其**声明的规范值**；研究者**不得**临时改写键来选择 Profile。
  这一义务由未来的选择服务与 Adapter 契约执行（ADR-0014「选择规则的权威性」延期义务的一部分），
  契约层不自报"已规范化"。

### D-26.5 `Ref` 的目标身份

- `Ref` 的**目标身份精确**为 `(kind, name, version)`，不含其 Contract 信封 `schema_version`。
- 跨对象判断"是否指向同一目标"必须使用目标身份，而不是 Pydantic 全结构相等。当前**至少**包括：
  - `LifecycleHistory` 构造校验与 `append` 中 `transition.subject` 与历史 `subject` 的比较；
  - `ExecutionModeChange` 切 LIVE 时 `risk_gate.subject`、`authorization.subject` 与变更 `subject` 的比较。
- 已经按规范串 `str(ref)` 比较的位置（`ReproducibilityTuple.dependency_hashes` 覆盖、
  `StrategyArtifact.dependencies` 覆盖）本就等价于目标身份，行为不变。

### D-26.6 不做全局改写

- `Contract` 的**结构相等**与**内容哈希**规则**不做**全局改写。
- 嵌套 `schema_version` 仍属于序列化载荷并参与内容哈希；不同信封版本得到不同内容哈希是**预期行为**。
- **不得**通过把版本从全局哈希载荷中排除来"修复"本问题：那会改变全部契约的内容身份，
  并破坏 ADR-0008 的逐模型排除表。

## 明确不做

- 不改变 `Contract.__eq__`、`content_hash()` 或任何模型的哈希排除表。
- 不对 `venue` / `symbol` / `timeframe` 做大小写或 Unicode 规范化，也不定义它们的取值集合。
- 不定义 `research_class` 的取值集合（D-09 H-7 仍开放）。
- 不实现选择服务、Registry 或 Adapter；不校验被选中的 Profile 是否已登记或 frozen。
- **其它值对象的同类比较不在本 ADR 决定**：例如 `DeploymentRecord` 与 `EquivalenceCheck` 之间
  `GitCodeRevision` 的比较（`core/domain/artifact.py:121`）同样包含信封版本。实施批次必须列出
  `core/` 中全部跨对象相等比较点，交 Codex 复核是否需要后续裁决；在裁决之前保持现状（fail closed）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 在需要"同一业务键 / 同一目标"的位置使用显式语义身份函数 | 精确修复 F1 / F3；不改变任何内容哈希；语义可测试 | 需要在每个跨对象比较点显式调用 | — |
| B 全局把 `schema_version` 排除出 `==` 与内容哈希 | 一次改完 | 改变全部内容身份；破坏 ADR-0008 排除表；信封版本变化将不可审计 | 代价远大于问题 |
| C 要求所有嵌套信封版本必须等于外层版本 | 消除分叉 | 与"同 major 更高 minor 可识别"（ADR-0010 §D-14）冲突；未来混合版本载荷被误拒 | 与既有决定冲突 |
| D 在键上做大小写 / Unicode 规范化 | 容错 | 在契约层隐式改写外部值，掩盖 Adapter 错误；规范化规则本身是未做的决定 | 超出 Phase 0 必要性 |
| E 不改，文档说明 | 零成本 | F1 是研究者自由度漏洞；发布后修复必须升 major | 违反 C-A4 |

## Schema 与迁移影响

- `ProfileSelectionKey.research_class` 增加 `pattern`，这是外部消费者可见的收紧。导出 Schema 中
  `ProfileSelectionKey` 以及内嵌它的 Schema（预期包括 `ProfileSelection`、`SelectionEntry`、
  `ProfileSelectionRule`、`ReproducibilityTuple`、`ExperimentSpec`、`ExperimentRun`、`ExperimentMetadata`）
  需要重新导出；实际范围以实施时的重导出结果为准。
- 语义身份函数与跨对象比较规则**不改变**任何字段、线格式或内容哈希算法。
- 不新增契约模型，`CONTRACT_MODELS` 数量不变（38），`V1_MODEL_NAMES` 不变。
- `schemas/v1/`（35 份）与 `tests/vectors/v1/`、`tests/vectors/v1_coverage/` **逐字节不变**；
  v1 只读路径不受影响。
- 仓库内无任何已登记的选择规则、实验或生命周期数据受影响。

### 版本（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，无任何 v2 数据登记。
因此本 ADR 是对同一个未发布版本的收窄，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。
**发布后做同类语义变化必须升 major。**

## 实施验收矩阵

> 本 ADR 尚为 Proposed；下列矩阵是获批后**实现批次**的验收条件。先写能暴露原缺陷的测试并记录 red。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | 同一规则中两个 key 业务字段完全相同、仅嵌套 `schema_version` 不同（映射到不同 Profile） | 判为重复，拒绝构造 |
| 2 | 同上但映射到同一 Profile | 同样判为重复（重复检测只看选择键身份） |
| 3 | 规则 entry 的 key 为 `2.0.0`，查询 key 为同 major 的不同信封版本（如 `2.0.1`、`2.0.0+build`） | 匹配同一 entry |
| 4 | 查询 key 的任一业务字段不同（含仅大小写不同） | 不匹配，抛 `ProfileViolation`，不回退 |
| 5 | `venue` 为 `binance` 与 `Binance` 的两条 entry | 接受，视为不同键（D-26.4 的明确边界） |
| 6 | `ProfileSelectionKey.research_class` 为 `Swing`、`swing-1`、`1swing`、空串 | 拒绝；`swing`、`swing_1` 接受 |
| 7 | 该约束与 `ProfileScope.research_class` 来自同一常量 | 是（源码或共享常量断言） |
| 8 | `LifecycleHistory` 直接构造与 `append`：转移 `subject` 与历史 `subject` 目标相同、信封版本不同 | 接受 |
| 9 | `ExecutionModeChange` → LIVE：`risk_gate.subject` / `authorization.subject` 与变更 subject 目标相同、信封版本不同 | 接受 |
| 10 | 上述比较中 `kind`、`name` 或 `version` 任一不同 | 仍拒绝 |
| 11 | 两个仅信封版本不同的 `Ref` / `ProfileSelectionKey` 的 `==` 与 `content_hash()` | 保持不同（D-26.6：不做全局改写） |
| 12 | 导出 Schema 中 `ProfileSelectionKey.research_class` | 带与 `ProfileScope.research_class` 相同的 pattern；current Schema 与重导出逐字节一致 |
| 13 | `core/` 中全部跨对象相等比较点清单 | 随实现提交列出，未决项（如 `GitCodeRevision`）交 Codex 复核 |
| 14 | `schemas/v1/`、`tests/vectors/v1/`、`tests/vectors/v1_coverage/` | 逐字节不变，旧哈希不变 |
| 15 | 四项工程检查 | 实际运行且全绿 |

## 后果

- 正面：Profile 选择恢复"确定性、唯一、研究者不可自选"的语义；跨对象的主体比较表达的是"同一目标"
  而不是"同一序列化文本"；内容身份规则不受波及。
- 负面 / 代价：每个跨对象比较点都要显式使用语义身份，新增比较点时必须遵守（由测试与复审把关）；
  `research_class` 收紧后，使用非标识符取值的调用方会被拒绝（仓库内无此类数据）。
- 对复现性的影响：内容哈希与 `experiment_hash` 的算法不变；v1 只读路径与旧哈希不变。

## 合规检查

- [ ] 契约变更经本 ADR 提出，获批前不实施
- [ ] 不修改 Validation Constitution 与 roadmap；动机与任何实验结果无关
- [ ] 不修改任何已接受 ADR 的正文
- [ ] Domain 层仍只依赖标准库与 Pydantic
- [ ] 未实现选择服务 / Registry / Adapter；未引入自报"已规范化"标志
