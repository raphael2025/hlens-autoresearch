# ADR-0018: 契约值对象的语义身份（D-26）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-24，Codex 依 Raphael 授权批准）**；尚待实施 |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 的项目技术决策授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文；C2b 按 Codex 最终裁决补齐 `GitCodeRevision` |
| 相关 Phase | Phase 0（批次 C2） |
| 影响范围 | Contract / Validation（Profile 选择）/ Lifecycle / 研究与生产边界 |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见「版本」一节） |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0009](0009-experiment-identity-binding.md)、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md)、[ADR-0015](0015-audit-identity-types-and-version-bindings.md) |
| 来源 | [Phase 0 关闭复审 C1](../reviews/2026-09-24-phase0-closing-review-c1.md) F1（P1）、F3（P2） |

## 背景

每个契约模型都继承 `Contract.schema_version`（`core/domain/base.py:302`）。嵌套的值对象因此也各自带一个
"信封版本"，它参与 Pydantic 的结构相等（`==`）与 `model_dump_json()`，也参与内容哈希。
C1 复审用探针证实了以下后果：

1. **F1（P1）Profile 选择的唯一映射可被绕过。** `ProfileSelectionRule._unique_keys`
   （`core/contracts/profile_selection.py:65-69`）用 `entry.key.model_dump_json()` 判重，
   `select()`（`:71-81`）用 `entry.key == key` 匹配。两个业务字段完全相同、只差嵌套
   `schema_version`（`2.0.0` / `2.0.1`）的 key 被当成两条不同条目，分别映射到 `strict` 与
   `lenient` Profile，规则被接受；查询方只要改写信封版本就能选中不同 Profile。
   这违反 Constitution C-A4 与 ADR-0007 §3 规则 5（Profile 由确定性映射选定，研究者不得自选）。
   同时 `ProfileSelectionKey.research_class`（`core/domain/selection.py:23`）只要求非空，
   而同一概念的 `ProfileScope.research_class`（`core/contracts/validation_profile.py:75`）有标识符约束。
2. **F3（P2）跨对象的"是否是同一个东西"用了全结构相等。**
   - 生命周期与 LIVE 证据的 subject 比较（`core/lifecycle/strategy.py:205,207,232,253`）用 `Ref` 的 `==`；
     两个 `str()` 完全相同、只差信封版本的 `Ref` 会被判为不同对象。
   - `DeploymentRecord._must_pass_equivalence`（`core/domain/artifact.py:116-123`）用 `!=` 比较
     自身与 `EquivalenceCheck` 的 `production_code_hash`（`GitCodeRevision`，`core/domain/base.py:376-391`）。
     [ADR-0015](0015-audit-identity-types-and-version-bindings.md) §D-21.2 已明确生产代码身份
     就是 commit + tree，且"相等性是结构化比较"；当前实现却被嵌套信封版本意外污染——同一 commit + tree、
     只差信封版本的两份修订会被判为"生产代码修订不一致"。这不是新的开放问题，而是既有决定的实现偏差。

这些比较都 fail closed（误拒）或可被操纵（F1），语义都不对。

## 决策（D-26）

本 ADR 给出**三类显式语义身份**的完整裁决。三者都**排除**各自的 Contract 信封 `schema_version`；
全局结构相等与内容哈希**保持不变**（D-26.7）。

| 语义身份 | 精确定义 | 用于 |
|---|---|---|
| Profile 选择键身份 | `(venue, symbol, timeframe, research_class)` | `ProfileSelectionRule` 的判重与 `select()` |
| `Ref` 目标身份 | `(kind, name, version)` | 生命周期 subject、LIVE 授权 / Risk Gate subject 的跨对象比较 |
| Git 代码修订身份 | `(commit_oid, tree_oid)` | `DeploymentRecord` 与 `EquivalenceCheck` 的生产代码修订比较 |

实施批次为每一类提供**一个**显式的身份函数（命名由实施批次确定，语义以本 ADR 为准），
所有列在 D-26.6 的比较点都必须调用它，不得在比较点就地拼接字段。

### D-26.1 `ProfileSelectionKey` 的选择键身份

- 选择键身份**精确**为 `(venue, symbol, timeframe, research_class)`。
- Contract 信封字段 `schema_version` **不参与**选择键身份。

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

### D-26.5 `Ref` 的目标身份与 `GitCodeRevision` 的代码身份

- `Ref` 的**目标身份精确**为 `(kind, name, version)`，不含其 Contract 信封 `schema_version`。
  跨对象判断"是否指向同一目标"必须使用目标身份，而不是 Pydantic 全结构相等。
- `GitCodeRevision` 的**代码身份精确**为 `(commit_oid, tree_oid)`，不含其 Contract 信封 `schema_version`。
  这是 ADR-0015 §D-21.2"生产代码身份 = commit + tree、结构化比较"的准确落点，不改变该决定。
  `DeploymentRecord` 比较自身与 `EquivalenceCheck` 的生产代码修订时**必须**比较代码身份，
  不得用 Pydantic 全结构相等。

### D-26.6 `core/` 跨对象比较点的完整盘点

基线 `76cd9e6` 上 `core/` 中全部 `==` / `!=` 比较点与跨对象成员判断如下。本 ADR **覆盖且只覆盖**
ProfileSelectionKey、Ref、GitCodeRevision 三类需要语义身份的比较；其余比较的对象是普通标量、
枚举或容器，**不受影响**。

| 位置 | 比较 | 类别 | 本 ADR 处置 |
|---|---|---|---|
| `core/contracts/profile_selection.py:65-67` | `entry.key.model_dump_json()` 判重 | 选择键 | **改用选择键身份**（D-26.2） |
| `core/contracts/profile_selection.py:73` | `entry.key == key` | 选择键 | **改用选择键身份**（D-26.2） |
| `core/lifecycle/strategy.py:205` | `risk_gate.subject != subject` | `Ref` | **改用目标身份** |
| `core/lifecycle/strategy.py:207` | `authorization.subject != subject` | `Ref` | **改用目标身份** |
| `core/lifecycle/strategy.py:232` | `append` 中 `transition.subject != subject` | `Ref` | **改用目标身份** |
| `core/lifecycle/strategy.py:253` | 构造校验中 `transition.subject != subject` | `Ref` | **改用目标身份** |
| `core/domain/artifact.py:121` | `equivalence.production_code_hash != production_code_hash` | `GitCodeRevision` | **改用代码身份** |
| `core/domain/research.py:197-211` | `dependency_hashes` 覆盖（`str(ref)` 集合差） | `Ref` 规范串 | 不变：`str(ref)` 本就等价于目标身份 |
| `core/domain/artifact.py:80` | `str(strategy_spec) in dependencies` | `Ref` 规范串 | 不变：同上 |
| `core/domain/artifact.py:119` | `equivalence.artifact_id != artifact_id` | `ContentHash` 字符串 | 不变 |
| `core/contracts/profile_selection.py:140` | `declared_research_class != key.research_class` | 字符串 | 不变 |
| `core/domain/specs.py:197` | `state_space` 去重长度 | 字符串元组 | 不变 |
| `core/domain/base.py:230` | `FrozenMapping.__eq__` | 映射内容 | 不变 |
| `core/domain/base.py:337` | `major != CONTRACT_SCHEMA_MAJOR` | 整数 | 不变 |
| `core/compat/v1.py:164` | `self._model != "ReproducibilityTuple"` | 字符串 | 不变 |
| 其余 `is` / `is not` / `in` 判断（`Kind`、`Zone`、`LifecycleState`、`ExecutionMode`、`Verdict`、转移集合） | 枚举与枚举元组 | 枚举 | 不变 |

实施批次必须在同一基线上复核此表；若发现表外的跨对象 Contract 比较点，停下交 Codex，而不是自行扩展。

### D-26.7 不做全局改写

- `Contract` 的**结构相等**（Pydantic `__eq__`）与**内容哈希**规则**保持不变**，这是有意的最终设计，
  不是留待未来改写的欠账。需要"同一业务键 / 同一目标 / 同一代码"语义的地方显式调用语义身份函数。
- 嵌套 `schema_version` 仍属于序列化载荷并参与内容哈希；不同信封版本得到不同内容哈希与不同结构相等结果
  是**预期行为**。
- **不得**通过把版本从全局哈希载荷或 `__eq__` 中排除来"修复"本问题：那会改变全部契约的内容身份，
  并破坏 ADR-0008 的逐模型排除表。

## 明确不做

- 不改变 `Contract.__eq__`、`content_hash()` 或任何模型的哈希排除表。
- 不对 `venue` / `symbol` / `timeframe` 做大小写或 Unicode 规范化，也不定义它们的取值集合。
- 不定义 `research_class` 的取值集合（D-09 H-7 仍开放）。
- 不改变 `GitCodeRevision` 的字段、格式约束或 ADR-0015 对 Git OID 的任何决定。
- 不实现选择服务、Registry 或 Adapter；不校验被选中的 Profile 是否已登记或 frozen；
  不校验 Git 对象是否存在。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 在需要"同一业务键 / 同一目标 / 同一代码"的位置使用三类显式语义身份函数 | 精确修复 F1 / F3；不改变任何内容哈希；语义可测试；比较点有完整清单 | 需要在每个比较点显式调用 | — |
| B 全局把 `schema_version` 排除出 `==` 与内容哈希 | 一次改完 | 改变全部内容身份；破坏 ADR-0008 排除表；信封版本变化将不可审计 | 代价远大于问题 |
| C 要求所有嵌套信封版本必须等于外层版本 | 消除分叉 | 与"同 major 更高 minor 可识别"（ADR-0010 §D-14）冲突；未来混合版本载荷被误拒 | 与既有决定冲突 |
| D 在键上做大小写 / Unicode 规范化 | 容错 | 在契约层隐式改写外部值，掩盖 Adapter 错误；规范化规则本身是未做的决定 | 超出 Phase 0 必要性 |
| E 不改，文档说明 | 零成本 | F1 是研究者自由度漏洞；发布后修复必须升 major | 违反 C-A4 |

## Schema 与迁移影响

- `ProfileSelectionKey.research_class` 增加 `pattern`，这是外部消费者可见的收紧。导出 Schema 中
  `ProfileSelectionKey` 以及内嵌它的 Schema（预期包括 `ProfileSelection`、`SelectionEntry`、
  `ProfileSelectionRule`、`ReproducibilityTuple`、`ExperimentSpec`、`ExperimentRun`、`ExperimentMetadata`）
  需要重新导出；实际范围以实施时的重导出结果为准。
- 三类语义身份函数与比较规则**不改变**任何字段、线格式或内容哈希算法；`Ref` 与 `GitCodeRevision`
  的导出 Schema 不因本 ADR 变化。
- 不新增契约模型，`CONTRACT_MODELS` 数量不变（38），`V1_MODEL_NAMES` 不变。
- `schemas/v1/`（35 份）与 `tests/vectors/v1/`、`tests/vectors/v1_coverage/` **逐字节不变**；
  v1 只读路径不受影响。
- 仓库内无任何已登记的选择规则、实验、生命周期或部署数据受影响。

### 版本（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，无任何 v2 数据登记。
因此本 ADR 是对同一个未发布版本的收窄，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。
**发布后做同类语义变化必须升 major。**

## 实施验收矩阵

> 本 ADR 已接受、**尚未实施**；下列矩阵是实现批次的验收条件。先写能暴露原缺陷的测试并记录 red。

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
| 10 | 上述 subject 比较中 `kind`、`name` 或 `version` 任一不同 | 仍拒绝 |
| 11 | `DeploymentRecord` 与其 `EquivalenceCheck` 的 `production_code_hash` 为同一 `commit_oid` + `tree_oid`、信封版本不同 | 接受 |
| 12 | 同上但 `commit_oid` 或 `tree_oid` 任一不同 | 仍拒绝 |
| 13 | 两个仅信封版本不同的 `Ref`、`ProfileSelectionKey`、`GitCodeRevision` 的 `==` 与 `content_hash()` | 保持不同（D-26.7：不做全局改写） |
| 14 | 导出 Schema 中 `ProfileSelectionKey.research_class` | 带与 `ProfileScope.research_class` 相同的 pattern；current Schema 与重导出逐字节一致 |
| 15 | D-26.6 比较点清单 | 实现提交在同一基线复核；表内七个语义比较点全部改用身份函数，表外无新增跨对象 Contract 比较 |
| 16 | `schemas/v1/`、`tests/vectors/v1/`、`tests/vectors/v1_coverage/` | 逐字节不变，旧哈希不变 |
| 17 | 四项工程检查 | 实际运行且全绿 |

## 后果

- 正面：Profile 选择恢复"确定性、唯一、研究者不可自选"的语义；生命周期与 LIVE 证据的主体比较表达的是
  "同一目标"，部署与等价检查比较的是"同一份代码"（ADR-0015 的原意），而不是"同一序列化文本"；
  内容身份规则不受波及。
- 负面 / 代价：每个语义比较点都要显式使用身份函数，新增比较点时必须遵守（由 D-26.6 清单、测试与复审把关）；
  `research_class` 收紧后，使用非标识符取值的调用方会被拒绝（仓库内无此类数据）。
- 对复现性的影响：内容哈希与 `experiment_hash` 的算法不变；v1 只读路径与旧哈希不变。

## 合规检查（已批准、尚待实施）

- [x] 契约变更经本 ADR 提出，并由 Codex 依 Raphael 授权接受；实施前不改代码
- [x] 不修改 Validation Constitution 与 roadmap；动机与任何实验结果无关
- [x] 不修改任何已接受 ADR 的正文（ADR-0015 的决定只被准确引用，未被改写）
- [ ] Domain 层仍只依赖标准库与 Pydantic —— 实施批次验证
- [ ] 未实现选择服务 / Registry / Adapter；未引入自报"已规范化"标志 —— 实施批次验证
- [ ] 验收矩阵 1 ~ 17 全部通过 —— 实施批次验证
