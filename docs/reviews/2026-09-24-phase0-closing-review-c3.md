# Phase 0 修复后关闭复验 C3（只读）

| 项目 | 内容 |
|---|---|
| 文档性质 | **历史审查记录**；不是状态文件、宪法、路线图或架构决定。结论只反映基线时的仓库状态，此后不回写 |
| 复验日期 | 2026-09-24 |
| 批次 | Phase 0 关闭复审批次 C 第三轮 C3（修复后复验），范围由 Codex 下达 |
| 基线 | 分支 `phase/0`，HEAD `4a2951ad3b7ad711e282b60952805a8f4dac480b`，复验开始与结束时工作树干净 |
| 复验者 | Claude Code（Opus 5.5），严格只读；未修改仓库、未提交、未使用子 Agent |
| 独立性说明 | C2c / C2d 的实现由同一 Claude 会话完成，本复验独立性有限；每项结论都以复验时新运行的命令与探针为依据，没有照抄实现批次的报告。最终裁决属 Codex |
| 固化 | 批次 C4a（docs-only）整理压缩后提交；结论、严重度与证据不变，修正项见 §10 |
| 结论 | **`READY_FOR_HUMAN_CONSTITUTION_GATE`** |

> 本结论**不**构成 Constitution 1.0.0 批准、Phase 0 关闭、main 合并或 tag。
> 复验时 Constitution 仍为 `0.2.0-draft`。

## 1. 基线

- 最近提交：`4a2951a`（ADR-0019 实施）← `9581773`（ADR-0018 实施）← `5fffb18`（两份 ADR Accepted）
  ← `76cd9e6`（C1 记录与 ADR 起草）← `fce4f81`（C1 基线）。
- 进入条件满足：ADR-0018 / 0019 均 Accepted，并分别在 C2c、C2d 两个独立提交中实施。
- 复验期间无仓库写入：`find -newermt <HEAD 提交时间>` 为空；所有产物都在 `/tmp`。

## 2. 结论

**`READY_FOR_HUMAN_CONSTITUTION_GATE`**：C1 的 `FIX_BEFORE_CLOSE` 条件已满足（F1 / F5 修复，F3 语义问题解决）；
无 P0 / P1 / P2；只剩 4 项 P3。剩余的是人工门：Constitution 1.0.0、Phase 0 正式关闭、main 合并、tag。

## 3. 发现

- **P0 / P1 / P2**：无。
- **P3**：
  1. **当前状态文字过时**（F6 同类，C2d 之后出现）：`PROJECT_STATUS.md:269` 仍写"证据最小结构待 ADR-0019 实施"；
     `docs/adr/README.md:85` 仍写 D-26 / D-27 "已接受、尚待实施"；`README.md:5` 与 `docs/research/roadmap.md:6`
     仍写"C2 修复进行中"。建议随状态同步提交修正，不阻塞关闭。
  2. **首尾空白处理在运行时与 JSON Schema 之间不完全一致**：契约全局开启 `str_strip_whitespace`，JSON Schema
     无法表达。纯空白串（如证据项 `"   "`，`core/lifecycle/strategy.py:115,129`）能通过 Schema 的
     `minLength: 1` 但运行时拒绝；`" swing "` 被 Schema 的 pattern 拒绝但运行时去空白后接受。
     这是全局既有行为，ADR-0010:65 只在版本号一处提过，`02-domain.md` 无通用说明。只需补文档。
  3. **`ExperimentMetadata.declared_research_class` 的直接 Schema 约束较弱**：字段本身只有 `minLength`
     （`core/contracts/profile_selection.py:111`），语义约束由运行时跨字段相等关系
     （`:142`，必须等于 `profile_selection.key.research_class`）间接保证，只读 Schema 的使用者看不到。
     这是 ADR-0018 D-26.3 的有意边界。
  4. **C1 遗留 P3 未处理**（不属修复范围）：`apps/` `strategies/` `risk/` 无 Python 文件，对它们的架构边界测试空转；
     模型校验器原样抛出 `LifecycleViolation` 而非 `ValidationError`；`ExperimentMetadata` 无 `run_id`。

## 4. C1 F1 ~ F6 处置

| ID | 处置 | 证据 |
|---|---|---|
| F1 | **CLOSED** | `core/contracts/profile_selection.py:67,74-75` 判重与查询都用 `selection_identity()`（`core/domain/selection.py:28`）。探针：同一业务键仅改 `schema_version`（2.0.1）规则被拒；用 `2.9.0+b` 查询仍选中 strict；`Binance` 与 `binance` 不匹配；`research_class="Swing"` 被拒；major 3 的 key 被拒 |
| F2 | **CLOSED（仅文档澄清）** | `git diff fce4f81 HEAD -- core/compat` 为 0 行，算法未改；`docs/architecture/10-migration.md:56-58` 写明"完整规范载荷"前提。探针：省略默认字段的载荷仍被接受并得到不同旧哈希，与文档一致。这是文档澄清，不是算法修复 |
| F3 | **CLOSED** | `core/lifecycle/strategy.py:218,220,245,266` 用 `target_identity()`（`core/domain/base.py:373`）；`core/domain/artifact.py:122-123` 用 `code_identity()`（`base.py:414`）。探针：同一目标 / 同一代码仅改信封版本，生命周期历史与部署记录接受；改名字或 tree 仍拒绝；`Ref` 与 `GitCodeRevision` 跨信封 `==` 为 False、内容哈希不同 |
| F4 | **CLOSED（已登记，仍未决）** | `docs/adr/README.md:79-82`、`PROJECT_STATUS.md:190-193`：D-28 ~ D-31 截止分别为 Phase 1 前 / 最迟 Phase 5 前 / Phase 4 前 / Phase 1 前，均注明"未决；不选方案" |
| F5 | **CLOSED** | `core/lifecycle/strategy.py:115,129`：`evidence: tuple[EvidenceRef, ...]`，至少一项、无默认值。探针：空、缺失、纯空白证据全部拒绝，`model_copy(update=...)` 也拒绝；同一人触发并批准仍接受；未加入证据真实性或职责分离检查 |
| F6 | **PARTIALLY_CLOSED（P3）** | C2a 修正的各处保持正确；ADR-0001 ~ 0017 正文自 `fce4f81` 起 0 行改动，0018 / 0019 自 `5fffb18` 起 0 行改动；剩余见 P3-1 |

`core/` 全部 `==` / `!=` / `in` 比较经 AST 扫描：7 处语义比较全部使用身份函数，其余均为字符串、枚举、整数、映射或状态二元组，无遗漏的跨对象 Contract 比较。

## 5. ADR-0018 / 0019 验收矩阵

期望值为独立字面量，未以实现自身为基准；抽查确认身份函数测试断言字面量元组、状态机边集与
`tests/test_lifecycle.py:103` 的独立期望表比对、red 阶段全部为断言失败（C2c：43 failed / 32 passed；
C2d：31 failed / 23 passed，见两个实现提交说明）。

| ADR-0018 # | 结果 | 证据（`tests/test_semantic_identities.py`） |
|---|---|---|
| 1、2 | PASS | `:93`、`:100` |
| 3 | PASS | `:107`、`:115` |
| 4 | PASS | `:135` |
| 5 | PASS | `:142` |
| 6 | PASS | `:158`、`:164`、`:171`、`:177` |
| 7 | PASS | `:189` |
| 8 ~ 10 | PASS | `:233`、`:243`、`:261`、`:291`、`:307` |
| 11、12 | PASS | `:324`、`:342` |
| 13 | PASS | `:374` |
| 14 | PASS | `:201` + 复验重导出逐字节一致 |
| 15 | PASS | `:422` + 复验独立 AST 扫描 |
| 16、17 | PASS | 见 §7、§9 |

| ADR-0019 # | 结果 | 证据（`tests/test_lifecycle_evidence.py`） |
|---|---|---|
| 1 | PASS | `:90`、`:95`、`:100`、`:107`、`:115`（18 条边） |
| 2 | PASS | `:133`、`:140` |
| 3 | PASS | `:151`（每条边在真实历史路径上走到） |
| 4 | PASS | `:170`、`:181` |
| 5 | PASS | `:192` |
| 6 | PASS | `:215` |
| 7 | PASS | `tests/test_lifecycle.py:103`、`:107` 与独立期望表比对 |
| 8 | PASS | `:234` |
| 9 | PASS | `:264`、`:272` + 复验重导出一致 |
| 10、11 | PASS | 见 §7、§9 |

另：`:240` 确认 `FailureRecord` / `RetirementRecord` 的 `evidence` 未被连带收紧。

## 6. Phase 0 roadmap 矩阵（`docs/research/roadmap.md` Phase 0）

| 项 | 判定 | 证据 |
|---|---|---|
| Constitution v1.0.0 已批准 | **HUMAN_GATE** | `constitution.md:5-6` 仍为 `0.2.0-draft` / Draft |
| Constitution 不含数值阈值 | PASS | 人工全文复核；文档一致性测试 |
| Profile、选择规则、Experiment Metadata 契约 | PASS | F1 已修复 |
| 核心实体契约与 Schema 导出 | PASS | `CONTRACT_MODELS` 38 = 已提交 38 = 新导出 38，名称一致 |
| 状态机只允许定义的转移 | PASS | 18 条边、3 个终态、6 条人工批准边与独立期望表一致；主体、时间、证据约束生效 |
| 错误分类 / 工程基线 / ADR 0003+ | PASS | ADR-0003 ~ 0019 全部 Accepted |
| 契约层无基础设施依赖 | PASS（P3-4） | 架构边界测试 |
| CI / 本地测试可运行 | 本地 PASS；CI **CORRECTLY_DEFERRED** | `git remote -v` 为空，不声称 CI 通过 |
| 禁止 Feature / Strategy / Backtest | PASS | 外层目录除 README 外文件数为 0 |
| 禁止接入真实数据 | PASS | `data/` 只有 README |
| 禁止引入 LLM | PASS | `uv.lock` 无 LLM 依赖；`LlmCall` 只是登记结构 |

## 7. Schema、v1 与迁移证据

- 重导出到 `/tmp/c3-schemas-*`：38 份与已提交文件逐字节一致（0 份差异）。
- 本地 `$ref`：current 177 个、v1 139 个，全部可解析，无非本地引用。
- C2 期间 Schema 变化均为预期：C2c 8 份（`research_class` 由 `minLength` 改为 pattern）；
  C2d 2 份（`LifecycleTransition` / `LifecycleHistory` 的 `evidence` 必填、`minItems: 1`、元素 `minLength: 1`）。
- v1 冻结资产在 C1 / C2 期间零字节变化：
  - 自 `fce4f81`、`5fffb18`、`9581773` 到 HEAD 的 Git 差异均为 0 行；相对 `cd84a4e` 的 55 行全部来自 ADR-0016 批次授权新增的 `tests/vectors/v1_coverage/`，`schemas/v1` 与 `tests/vectors/v1` 为 0 行；
  - 44 个文件的 blob 清单与 `fce4f81` 完全相同；
  - 随机抽查（seed 20260924）8 个文件 sha256 全部一致；
  - 35 份 v1 快照与 `066b22d` 导出逐字节一致；向量文件与创建它们的提交（`4f83e18`、`1ad9f59`）逐字节一致。
- 旧版本读取：6 个固定向量（5 个 v1 + 1 个 coverage）旧哈希不变；v1 载荷仍被 v2 模型拒绝；v1 / v2 哈希不可比较。
- 真实外部 v1 数据迁移：**NOT ENOUGH EVIDENCE**。

## 8. Constitution 人工门审阅

**无阻止批准的内容问题。** 全文是纯原则、无数值阈值，自 `fce4f81` 起 0 行改动；与契约 / ADR 一致
（C-A7 五类门槛对应 Profile 五个必填参数组；C-A3 / C-A5 / C-P4 对应 Profile 引用 + 内容哈希绑定；
C-G1 ~ C-G6 与 ADR-0006 / 0011 一致；C-L4 / C-L5 执行点已登记为 D-31 / D-30）。

供参考（不阻塞）：

1. 批准程序：按第九章，批准时需更新版本号、状态行与修改历史，并留下批准记录（ADR 或决定记录）。
2. 可选措辞澄清：A6 的"资金费率"对现货不适用，可考虑"适用时"（与未决的 H-6 相关，可留待那时）；
   C-G2 只列出 PRODUCTION_CANDIDATE → ACTIVE，完整批准集合经 C-G1 引用 ADR-0006，不矛盾。

## 9. 实际检查（原样摘要）

| 检查 | 结果 | 退出码 |
|---|---|---|
| `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync --offline pytest -p no:cacheprovider -q` | `1433 passed in 1.17s` | 0 |
| `ruff check .` | `All checks passed!` | 0 |
| `ruff format --check .` | `114 files already formatted` | 0 |
| `mypy`（strict） | `Success: no issues found in 35 source files` | 0 |
| `git diff --check` | 无输出 | 0 |
| `git status --short` | 空 | — |
| 临时目录重导出逐字节比较 | 38 份一致，0 差异 | 0 |
| v1 冻结路径 Git 差异与哈希清单 | 相对 `fce4f81` 0 行；44 文件清单相同；抽查 8 个全部 OK | 0 |

ruff / mypy 缓存重定向到 `/tmp`。

## 10. 固化修正说明

- P3-3 原报告引用 `core/contracts/profile_selection.py:109`；该行实际是 `trial_index`，
  `declared_research_class` 位于 `:111`，已改为 `:111`。**不影响结论。**
- 其余内容仅压缩了措辞与命令日志，未改变结论、严重度或证据。

## 11. 风险与下一步（复验时）

- 风险（均已诚实延期）：生命周期证据的存在性与内容、`approved_by` 真实性与职责分离属未来授权服务；
  `venue` / `symbol` / `timeframe` 区分大小写（ADR-0018 有意设计）；契约层不再拒绝 LIVE，Phase 13 红线靠人与流程；
  D-28 ~ D-31 与 D-01 / 02 / 04 / 08 / 10 各有截止 Phase。
- 下一步：Codex 复核；可选的 docs-only 状态同步（P3-1、P3-2）；Constitution 1.0.0、Phase 0 关闭、
  main 合并与 `phase-0-complete` tag 的人工门；远程仓库决定。
- `ARCHITECTURE_DECISION_REQUIRED`：NO（剩余的是人工批准门，不是架构冲突）。

## 12. 后续处置（固化时补记，不属于 C3 原结论）

- P3-1、P3-2 已在批次 C4a 的当前文档中处理（状态文字同步；`02-domain.md` §3.7 说明字符串校验边界）；
  P3-3 保持为 ADR-0018 的有意边界，不扩张 Schema；P3-4 未处理。
- Constitution 1.0.0 的发布以 [ADR-0020](../adr/0020-approve-research-constitution-v1.md)（C4a 时为 Proposed）提出。
