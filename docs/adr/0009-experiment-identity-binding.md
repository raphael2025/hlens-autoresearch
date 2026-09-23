# ADR-0009: 实验规格身份、运行标识与依赖内容绑定

| 字段 | 值 |
|---|---|
| 状态 | **Proposed** |
| 日期 | 2026-09-23 |
| 决策者 | Raphael（待批准） |
| 起草者 | Claude Code（Opus）起草；Codex 文档审查整理 |
| 相关 Phase | Phase 0 |
| 影响范围 | Contract / Data（实验复现机制） |
| 是否破坏兼容 | **是**（字段搬迁 + 语义变化；与 ADR-0008 共同构成未发布的 `2.0.0`） |
| 前置条件 | ADR-0008 与本 ADR 均获批后串行实施；先完成 ADR-0008 的载荷基础，最后统一完成 v2 发布验收 |

## 背景

1. `core/domain/research.py:124-125` 把 `experiment_hash` 定义为复现元组内容哈希，与 `06-experiment.md:41` 的冻结定义一致；但 `ExperimentSpec.strategy / risk_policy / outcome`（`research.py:133-135`）在元组之外，**两个引用不同策略的实验可得到相同 `experiment_hash`**。这是冻结契约的覆盖缺口，只能经 ADR 修改。
2. `06-experiment.md:6` 写"一个 Spec 可以有多次 Run（复现检查、不同随机种子）"，而 `seeds` 在元组内（`research.py:107`），改种子即改哈希。需要一次明确修订。
3. 绑定缺失：`ValidationReport`（`research.py:192-203`）不记录 `run_id`；`ExperimentRun` 自带整份 `repro` 副本（`:157`）却不与 `experiment: Ref` 交叉校验；`experiment`、`strategy/risk_policy/outcome` 的 `kind` 均未校验。
4. 覆盖规范缺失：`plugin_versions`（`research.py:102`）与 `StrategyArtifact.dependencies`（`artifact.py:35`）的 `dict[str,str]` **能够**表达 `name@version → content_hash`（`06-experiment.md:19`、`0005:64`），缺的是键值格式规范与覆盖校验；`profile_selection`（`research.py:112`）默认空字典，且只有规则版本、没有唯一规则引用与哈希（`06-experiment.md:26`）。
5. `LlmCall`（`research.py:86-93`）只有三个哈希，而 `06-experiment.md:29` 要求"完整输入输出"。

## 决策

**1. 规格身份：补全复现元组，`experiment_hash` 定义文字不变**

- `ReproducibilityTuple` 新增 `strategy_ref: Ref | None`、`risk_policy_ref: Ref | None`、`outcome_ref: Ref | None`，均需显式提供；不适用时明确填 null，不以缺省掩盖遗漏。
- `ExperimentSpec` 的同名字段改为**从 `repro` 派生的只读引用**，消除同一信息两处存放且可不一致的结构。
- 补齐 kind 校验：`strategy_ref.kind is STRATEGY`、`risk_policy_ref.kind is RISK`、`outcome_ref.kind is OUTCOME`、`ExperimentRun.experiment.kind is EXPERIMENT`。
- `experiment_hash` 仍是"复现元组规范化 JSON 的 SHA-256"（`06-experiment.md:41` 文字不变），但覆盖面变宽 → 与 v1 的同名值**不可比较**。

**2. seeds：保留在元组与哈希内（小方案）**

- 实际 `seeds` 保留在 `ReproducibilityTuple`，参与 `experiment_hash`。
- **相同完整规格（含相同 seeds）重复运行 → 同一 `experiment_hash`**，满足复现检查。
- **不同实际 seeds → 新的不可变规格变体、新的 `experiment_hash`**，通过 `hypothesis_family_id` 与 `trial_index / family_trial_count`（`core/contracts/profile_selection.py:87-89`）保持关联与尝试计数。
- 本 ADR 明确修订 `06-experiment.md:6` 的"同一 Spec 不同随机种子"表述为上述语义。**不引入** `seed_policy` 或任何种子派生 DSL。

**3. 运行标识**

`run_id`（`research.py:153`）是**每次尝试唯一的不透明标识**，由未来 Runner 生成；本 ADR **不冻结其生成算法**，也不将其定义为内容哈希。契约层新增的约束仅为：`ExperimentRun.experiment` 的 kind 校验；`run.repro.experiment_hash` 与所引用 Spec 的一致性校验**属 Runner / Registry 义务**（契约内拿不到 Spec 实例），本轮只写条款。

**4. 结果身份**

本轮**不**定义结果内容身份。`report_id`（`research.py:193`）是外部赋予的标识，**不得**称为结果内容身份。本 ADR 仅规定未来方向：结果身份应由内容寻址的结果 manifest 提供，实现不在本轮。不新增 `ValidationReport` 与 `ExperimentRun` 的通用字段排除规则；其原有引用、审计信息及新增语义字段继续参与既有内容哈希流程。字段和 schema_version 改变自然会改变哈希，不要求 v1/v2 值相等。

**5. 绑定与覆盖规则（具体，非"看起来结构化"）**

| 项 | 规则 |
|---|---|
| `ReproducibilityTuple.dependency_hashes` | 新增 JSON object，Python 侧采用 ADR-0008 的只读 Mapping；键为 `kind:name@version` 规范串，值为 SHA-256 内容哈希。**直接覆盖规则**：`hypothesis_ref`、`strategy_ref`、`risk_policy_ref`、`outcome_ref`、`cost_model_ref` 中所有非空引用必须出现，缺一即拒绝。Profile 与选择规则由各自专用哈希字段绑定；Dataset 仍按已冻结快照身份引用 |
| `plugin_versions` | 保持 `dict[str, str]`；书面规定键为 `name@version`、值为 `content_hash`，并加格式校验（不重新结构化） |
| `StrategyArtifact.dependencies` | 保留 JSON object 表达；键使用 `kind:name@version`，避免 Feature/State 等同名对象混淆；值为 SHA-256 内容哈希。Artifact 的直接 strategy_spec 引用必须有内容绑定；完整传递依赖闭包由未来 Registry/打包器解析并检查 |
| `profile_selection` | 改为必填结构：Profile 选择规则的**唯一规则引用 + 规则内容哈希 + `ProfileSelectionKey`**；取消空默认值；规则引用 kind 必须为 profile_selection_rule。共享值对象应放在不会形成 research/profile_selection 循环导入的位置 |
| `ValidationReport` | 新增 `run_id`（必填），与既有 `experiment_hash` 同时存在，形成 spec ↔ run ↔ report 可核验链 |
| `SelectionEntry`（`profile_selection.py:35-38`） | 校验 `profile.kind is PROFILE` 且 `profile.version == profile_version` |
| `LlmCall` | **登记缺口**：`06-experiment.md:29` 要求完整输入输出，当前仅存哈希。本 ADR 记录该义务，**完整内容或可取回引用的实现在存储层就位后完成**，本轮不改字段 |

**6. Runner / Registry 义务（本轮不实现、不加自报字段）**

`params` 的默认值展开完整性（`06-experiment.md:20`）、`run.repro` 与 Spec 的一致性、trial 计数与权威账本一致（R11）、同 `name@version` 不同内容的拒绝（ADR-0008 决策 7），以及 Feature/State/Event 等传递依赖的解析、闭包完整性与内容校验。直接引用覆盖检查只是必要条件，不能表述为已验证完整依赖图。**不引入任何自报布尔标志**。

**7. 版本与旧记录边界**

- 与 ADR-0008 共同构成未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；两批实现之间不发布、不登记 v2 实验；版本号在两者都完成后一次提升。
- 保留 **v1 只读路径**：沿用 ADR-0008 定义的 Schema 快照、可执行只读入口与 v1 哈希语义至少一个 major。旧载荷按 v1 语义读取，**不得**用 v2 算法重算并覆盖；未知 major 拒绝，新登记只接受 v2 路径。
- **缺关键绑定（`dependency_hashes` 覆盖不全、无 `run_id`、`profile_selection` 为空）的旧实验，不得自动获得 v2 的可晋升资格**；若需晋升必须重新登记。
- 仓库内未发现持久实验；外部历史数据存在性 **NOT ENOUGH EVIDENCE**；不触碰旧项目。

**8. 文档同步（不修改任何 Accepted ADR 正文）**

在本 ADR 内说明对 `06-experiment.md:6` 的修订与对 ADR-0005 §2 依赖表达的补充；仅更新 `docs/adr/README.md` 索引与 `06-experiment.md`、`02-domain.md` 架构文档。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（推荐，本次小方案）** 元组补全 + seeds 留在哈希内 + 不透明 `run_id` | 改动面最小且语义唯一；不冻结未来 Runner 算法 | 不同 seeds 产生多个规格变体，族内规格数量增加（由 trial 计数吸收） | — |
| B `seed_policy` + 确定性 `run_id` | 同一规格可跨种子聚合 | 引入派生 DSL 与算法冻结，超出 Phase 0 必要性 | 已按审查意见撤回 |
| C 新增 `spec_hash`，保留旧 `experiment_hash` 含义 | 可以保留旧字段，但依赖方仍需明确迁移 | 长期两个相近哈希并存，绑定易错；不能笼统宣称无破坏 | 长期歧义成本更高 |
| D 不改契约，由未来 Runner 自行拼身份 | Phase 0 零改动 | 身份规则散落实现，违反 P1/P4，无契约级反例 | 不可验证 |

## 后果

- 正面：引用不同策略/风险/结果的实验不再哈希碰撞；report 可回指 run；依赖内容进入身份。
- 负面 / 代价：一次 major；`ExperimentSpec` 调用形态改变；`tests/factories.py` 需同步；不同 seeds 的规格数量增加。
- 需要迁移的内容：重导出 `schemas/`（差异以实测为准）；保留 v1 只读快照；`06-experiment.md` 修改历史需注明 `experiment_hash` 覆盖面断点。
- 对复现性的影响：v1 与 v2 的 `experiment_hash` 不可比较，必须书面注明；仓库内无历史实验受影响。

## 验收矩阵（先写测试、先记录 red，再实现）

| # | 反例 | 期望 |
|---|---|---|
| 1 | 分别只改变策略、风控、Outcome 引用；或同一引用的内容绑定 | 每种变化均改变 `experiment_hash`；旧版本反例使用旧 API，不以新字段不存在导致的构造报错充当 red 证据 |
| 2 | 两个独立构造、内容相同的元组（含相同 seeds） | `experiment_hash` 相同 |
| 3 | 仅 `seeds` 不同 | `experiment_hash` 不同，`hypothesis_family_id` 可保持一致 |
| 4 | `strategy_ref` 传 `Kind.FEATURE` / `experiment` 传非 experiment kind | 均拒绝 |
| 5 | `dependency_hashes` 未覆盖某个非空引用 | 拒绝 |
| 6 | `plugin_versions` 键不符合 `name@version` | 拒绝 |
| 7 | `profile_selection` 缺规则引用或规则哈希 | 拒绝 |
| 8 | `ValidationReport` 缺 `run_id` | 拒绝 |
| 9 | `SelectionEntry` 中 `profile.version != profile_version` | 拒绝 |
| 10 | 元组 JSON 往返（`model_dump_json` → `model_validate_json`） | `experiment_hash` 按位一致 |
| 11 | Report 的 run_id 或 Run 的语义绑定变化 | 不会被通用 `*_id` 排除规则吞掉；不要求 v1/v2 哈希相等 |
| 12 | v1 固定载荷通过只读入口读取 | 保留旧身份；缺失绑定的旧记录不能直接作为 v2 登记/晋升输入 |
| 13 | 仅参数、数据快照、cost_model、Profile/选择规则、插件内容绑定变化 | 对应实验哈希改变；字典插入顺序变化不改变哈希 |

## 合规检查

- [x] 已说明 major 版本、v1 只读保留与旧记录晋升边界
- [x] 不修改 Validation Constitution（C-P1/C-P4 得到更强落实，文字不动）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变
- [x] 不修改任何 Accepted ADR 正文

## 实现范围

`core/domain/research.py`、`core/domain/artifact.py`、`core/contracts/profile_selection.py` 及必要共享值对象、`core/contracts/registry.py`、`core/domain/base.py`（仅统一 v2 版本与兼容分派所需）、v1 只读入口、相关契约/验证测试与固定向量、Schema 快照与导出、`02-domain.md`、`06-experiment.md`、ADR 索引和项目状态。不得在该范围名义下实现 Registry 服务、实验 Runner、存储或生产执行。
