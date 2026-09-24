# ADR-0015: 审计身份类型与版本绑定（D-21、D-22）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-24，Codex 依 Raphael 授权批准） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（Raphael 已授权其决定项目技术方向） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 0（批次 B3） |
| 影响范围 | Contract / 实验复现机制 / 研究与生产边界 |
| 是否破坏兼容 | 否（收紧尚未发布的 `CONTRACT_SCHEMA_VERSION = 2.0.0`；不升 major，理由见 §5） |
| 前置 | [ADR-0005](0005-research-production-boundary.md)、[ADR-0009](0009-experiment-identity-binding.md)、[ADR-0010](0010-contract-construction-and-canonical-versioning.md) |

## 背景

ADR-0010 §D-16 已经把**依赖表**的键值格式标注到字段类型上（`RefKey` / `PluginKey` /
`ContentHash`），运行时与 JSON Schema 同源。但**顶层的审计身份字段**没有一起收敛，
目前仍是 `str = Field(min_length=1)` 或 `min_length=7`：

1. **内容哈希字段没有类型。** `ValidationReport.experiment_hash` /
   `validation_profile_hash`（`core/domain/research.py:300,303`）、
   `ExperimentMetadata` 的同名字段（`core/contracts/profile_selection.py:88,91`）、
   `ReproducibilityTuple.validation_profile_hash`（`research.py:140`）、
   `GoldenOutputs.signals_hash / positions_hash`（`core/domain/artifact.py:31,33`）、
   `EquivalenceCheck.artifact_id` 与 `DeploymentRecord.artifact_id / config_hash`
   （`artifact.py:75,90,92`）在文档中都被定义为规范化 JSON / 内容的 SHA-256
   （`02-domain.md` §1、§3.1，`ADR-0005` §3），但契约只要求"非空字符串"。
2. **Git OID 与内容哈希混在同一种表达里。** `ReproducibilityTuple.code_commit`
   （`research.py:125`，`min_length=7`）允许短 SHA；`StrategyArtifact.research_code_commit /
   research_code_tree_hash`（`artifact.py:47-48`）同样；`production_code_hash`
   （`artifact.py:76,91`）名为 hash，实际是 ADR-0005 §3 定义的"commit + tree hash"复合概念，
   却只有一个字符串槽位。短 SHA 在仓库增长后会碰撞，也无法区分 commit 与 tree。
3. **Profile 的身份被塞进 `*_version` 字符串。** `validation_profile_version`
   （`research.py:139`、`:302`、`profile_selection.py:90`）按 `06-experiment.md` §2 要被写成
   `vp:{scope_id}@{semver} + content_hash` 这样的复合引用，却是一个自由字符串；
   `constitution_version`（`research.py:138`、`:301`）同样无语法约束。
   `ExperimentMetadata` 还把选择依据拆成 `profile_selection_rule_version` +
   `profile_selection_key` 两个字段（`profile_selection.py:92-93`），
   与 `ProfileSelection` 这一已有值对象重复且更弱（缺规则内容哈希）。
   `SelectionEntry.profile_version`（`profile_selection.py:34`）与 `profile.version` 重复，
   只能靠一条校验维持一致。

## 精确决定

### D-21.1 统一的 `ContentHash`

**语义上已明确定义为"canonical JSON / 内容的 SHA-256"的字段，一律使用统一的 `ContentHash`
标注类型**（64 位小写十六进制，与 `core/domain/base.py:150` 的既有定义同源）：

| 模型 | 字段 |
|---|---|
| `ReproducibilityTuple` | `validation_profile_hash` |
| `ValidationReport` | `experiment_hash`、`validation_profile_hash` |
| `ExperimentMetadata` | `experiment_hash`、`validation_profile_hash` |
| `StrategyArtifact` | `experiment_hashes` 的每一项 |
| `GoldenOutputs` | `signals_hash`、`positions_hash` |
| `EquivalenceCheck` | `artifact_id` |
| `DeploymentRecord` | `artifact_id`、`config_hash` |

`artifact_id` 按 ADR-0005 §3 定义为 manifest 规范化 JSON 的 SHA-256，
因此它属于本表而不属于"不透明 ID"。

### D-21.2 Git OID 与 ContentHash 分离

新增标注类型 `GitOid`：**小写十六进制，长度为 40（SHA-1）或 64（SHA-256）**，不接受短 SHA。
Git 对象 ID 与本项目的内容哈希是两套命名空间，不得共用同一类型。

| 位置 | 决定 |
|---|---|
| `ReproducibilityTuple.code_commit` | `GitOid` |
| `StrategyArtifact.research_code_commit` | `GitOid` |
| `StrategyArtifact.research_code_tree_hash` | `GitOid` |

**生产代码身份改为结构化值对象**：新增 `GitCodeRevision { commit_oid: GitOid, tree_oid: GitOid }`。
`EquivalenceCheck` 与 `DeploymentRecord` 的生产代码身份使用**同一个值对象**，
两者相等性比较因此是结构化比较，而不是字符串比较。这与 ADR-0005 §3 对 `code_hash`
"commit + tree hash"的定义一致。`DeploymentRecord._must_pass_equivalence`
（`artifact.py:95-103`）中的一致性校验继续存在，比较对象改为该值对象。

### D-21.3 不按名称机械收紧的字段

以下字段**保持现状**，不因名字里有 `id` / `hash` / `uri` 就套用上述类型：

| 字段 | 原因 |
|---|---|
| `run_id`、`report_id`、`deployment_id`、`trace_id` | 不透明标识，生成算法未冻结（ADR-0009 §3、§4） |
| `DatasetRef.snapshot_id` | Iceberg 快照标识，格式由外部系统决定 |
| `GoldenOutputs.signals_uri / positions_uri` | URI，格式取决于未定的存储方案（D-01、D-02） |
| `StrategyArtifact.validation_reports` | 报告 ID 列表，即 `report_id`，不是内容身份 |
| `ReproducibilityTuple.environment_lock` | 仍是"锁文件哈希 + Python 版本 + 平台"的**复合描述**；其结构化表达**后续另定**，本轮不定义 |

### D-22.1 `constitution_version` 使用唯一 ASCII SemVer

`constitution_version`（`ReproducibilityTuple`、`ValidationReport`、`ExperimentMetadata`）
使用 ADR-0010 §D-14 的**唯一 ASCII SemVer 2.0.0 语法**（`SEMVER_PATTERN`），与全项目共用一套组件。
Constitution 当前为 `0.2.0-draft`——prerelease 形式合法，本约束不预判它何时成为 `1.0.0`。

### D-22.2 Profile 绑定改为 Ref + ContentHash

**不再把复合引用塞进 `*_version` 字符串。** 以下三个模型的
`validation_profile_version: str` 改为 `validation_profile: Ref`（`kind` 必须是 `profile`），
与既有的 `validation_profile_hash: ContentHash` 配对：

- `ReproducibilityTuple`
- `ValidationReport`
- `ExperimentMetadata`

"版本 + 内容哈希"的绑定语义不变（`06-experiment.md` §2：运行前绑定，运行后不可更换），
改变的是它的表达：版本号从自由字符串变为已校验的 `Ref`。

### D-22.3 `ExperimentMetadata` 使用完整 `ProfileSelection`

`ExperimentMetadata` 的 `profile_selection_rule_version` 与 `profile_selection_key`
两个字段，替换为一个 `profile_selection: ProfileSelection`
（`core/domain/selection.py`：selection-rule `Ref` + 规则内容哈希 + `ProfileSelectionKey`）。

理由：`ProfileSelection` 已经是复现元组使用的值对象，且更强——它带规则的内容哈希。
两处用同一个值对象，事后重建"按哪条规则、按什么输入选中了哪个 Profile"才有唯一答案。
既有的"`declared_research_class` 必须等于选择输入的 `research_class`"校验
（`profile_selection.py:111-114`）继续存在，读取路径改为 `profile_selection.key.research_class`。

### D-22.4 `SelectionEntry` 删除重复字段

`SelectionEntry.profile_version` 删除；版本从 `profile.version` 读取。
`profile.kind is PROFILE` 的校验保留；"版本必须一致"的校验随重复字段一并消失
（不存在两个可以不一致的副本）。

### D-21.4 v1 边界

`schemas/v1/`（35 份）与 `tests/vectors/v1/` 的固定载荷与旧哈希**逐字节不变**。
v1 与 v2 的 `content_hash` / `experiment_hash` 本就**不可比较**（ADR-0009 §7），
本 ADR 扩大了 v2 侧的字段类型变化，不改变这一结论，也不追溯重算任何 v1 身份。

## 明确不做

- 不定义 `run_id` / `report_id` / `deployment_id` 的生成算法（ADR-0009 §3、§4 的边界不变）。
- 不定义结果内容身份（内容寻址的结果 manifest 仍留待未来）。
- 不定义 `environment_lock` 的结构化表达。
- 不定义 URI 方案、存储布局或对象命名。
- 不校验任何哈希是否与真实内容一致，也不校验 Git OID 是否存在于任何仓库。
- 不改变 `experiment_hash` 的定义文字（`06-experiment.md` §3：复现元组规范化 JSON 的 SHA-256）。
- 不改变 Constitution 的版本号，也不批准它升到 `1.0.0`。
- 不引入 Git 库依赖；`GitOid` 只是格式约束。

## 运行时延期义务

| 义务 | 说明 |
|---|---|
| 哈希与内容一致 | 某个 `ContentHash` 是否真的等于被引用对象的内容哈希，需持有对象实例，属 Registry |
| Git 对象存在性 | `commit_oid` / `tree_oid` 是否存在、工作区是否干净，属 Runner 与打包器 |
| Profile / 规则存在性 | `validation_profile` 指向的版本是否已登记、是否 frozen，属 Control Plane |
| 跨模型一致 | Run、报告、元数据三处的 Profile 绑定是否彼此一致，契约层拿不到另外两个实例 |
| `environment_lock` 的可复现性 | 锁文件哈希是否对应可重建的环境，属 Runner |

## Schema 与迁移影响

- 受影响模型：`ReproducibilityTuple`、`ValidationReport`、`ExperimentMetadata`、
  `SelectionEntry`、`StrategyArtifact`、`GoldenOutputs`、`EquivalenceCheck`、`DeploymentRecord`。
- **新增契约模型 `GitCodeRevision`**：必须登记进 `CONTRACT_MODELS`，导出新的 Schema 文件，
  并计入"所有核心实体都有契约与 Schema 导出"这条验收标准。
  它是 v2 才出现的模型，**不进入 `V1_MODEL_NAMES`**。
- `ExperimentMetadata` 与 `SelectionEntry` 有字段删除与替换：这是尚未发布的 v2 内部调整，
  不存在需要迁移的 v2 数据。
- 需要重新导出 `schemas/`，顶层 Schema 数量将从 36 份增加；实际数量与差异以实施时的重导出结果为准。
- `schemas/v1/`（35 份）与 `tests/vectors/v1/` **逐字节不变**。
- **文档同步义务（随本 ADR 的实现批次执行，接受本 ADR 的提交不做）**：`06-experiment.md` §2 与
  `07-validation.md` §5 目前把 Profile 引用写成 `vp:{scope_id}@{semver}`，
  与 `Ref(kind=profile)` 的规范串 `profile:{name}@{semver}` 不是同一种写法，需一并修订；
  `02-domain.md` §3.2 的身份键表需要补入本 ADR 的顶层身份类型。

### 为什么仍是 2.0.0（D-25）

`2.0.0` **尚未发布**：只存在于 `phase/0` 分支，未合并 `main`，无 tag、无远程发布，
也没有任何 v2 数据登记。因此即使本 ADR 含字段删除与类型替换，它仍是对同一个未发布版本的
收窄，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`。**若在发布之后做同类改变，则必须升 major。**

## 验收测试矩阵

> 本 ADR 已获批准；下列矩阵是**实现批次**的验收条件。实现尚未发生，矩阵未运行。

| # | 反例 / 场景 | 期望 |
|---|---|---|
| 1 | D-21.1 表中每个字段传非 64 位十六进制串（含大写、63/65 位、非十六进制字符） | 逐字段拒绝 |
| 2 | 同上传合法的 64 位小写十六进制 | 接受 |
| 3 | `code_commit` 传 7 位短 SHA | 拒绝 |
| 4 | `code_commit` 传 40 位、64 位小写十六进制 | 接受 |
| 5 | `code_commit` 传含大写的 40 位串 | 拒绝 |
| 6 | `GitCodeRevision` 的 `commit_oid` / `tree_oid` 传非法 OID | 拒绝 |
| 7 | `DeploymentRecord` 与 `EquivalenceCheck` 的生产代码身份不一致 | 拒绝（既有校验按值对象比较） |
| 8 | `run_id` / `report_id` / `snapshot_id` / URI / `environment_lock` 传非哈希字符串 | **接受**（未按名称收紧） |
| 9 | `constitution_version` 传 `1.0`、`v1.0.0`、`١.٠.٠`、`01.0.0` | 全部拒绝 |
| 10 | `constitution_version` 传 `0.2.0-draft`、`1.0.0` | 接受 |
| 11 | `validation_profile` 传 `kind != profile` 的 `Ref` | 拒绝 |
| 12 | 载荷中仍出现 `validation_profile_version` 字符串字段 | 拒绝（`extra="forbid"`） |
| 13 | `ExperimentMetadata` 载荷中仍出现 `profile_selection_rule_version` / `profile_selection_key` | 拒绝 |
| 14 | `ExperimentMetadata` 的 `declared_research_class` 与 `profile_selection.key.research_class` 不一致 | 拒绝（既有校验保留） |
| 15 | `SelectionEntry` 载荷中仍出现 `profile_version` | 拒绝 |
| 16 | `GitCodeRevision` 是否已登记进 `CONTRACT_MODELS` 并导出 Schema | 是 |
| 17 | `V1_MODEL_NAMES` 是否包含 `GitCodeRevision` | 否 |
| 18 | `schemas/v1/` 35 份快照与 `tests/vectors/v1/` 固定向量 | 逐字节不变，旧哈希不变 |
| 19 | v1 与 v2 的同名哈希 | 不做相等断言；文档写明不可比较 |

## 后果

- 正面：审计链上的每个身份槽位都有可机读的格式，外部消费者能从 Schema 读到真实规则；
  Git 身份不再能用短 SHA 或与内容哈希混淆；Profile 绑定从自由字符串变为已校验引用；
  选择依据只有一处表达，事后重建不再依赖两个可能不一致的副本。
- 负面 / 代价：调用方必须提供完整 OID 与规范化哈希，测试工厂与固定向量需同步；
  新增一个契约模型与一份 Schema；`06-experiment.md` / `07-validation.md` 的 `vp:` 写法
  需要在实现时一并修订。
- 对复现性的影响：v1 只读路径与旧哈希逐字节不变；v2 侧的 `experiment_hash` 会因字段
  结构变化而改变，但 v2 尚未发布、无数据登记，不存在需要迁移的历史实验。

## 合规检查

- [ ] 契约变更经本 ADR 提出，未擅自修改冻结契约
- [ ] 不修改 Validation Constitution 与 roadmap
- [ ] 不修改任何已批准 ADR 的正文
- [ ] Domain 层仍只依赖标准库与 Pydantic（未引入 Git 库）
- [ ] 未实现 Registry / Runner / 存储；未引入自报布尔标志
