# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution**（进行中） |
| 当前子阶段 | B3 的七份 ADR 已通过 Codex 文档复核并**全部 Accepted**；串行实现已授权，**尚未开始** |
| 总体状态 | 🔄 进行中 |
| 最后更新时间 | 2026-09-24 |

独立审查发现的不可变性与实验身份问题已由 B1（ADR-0008）、B2（ADR-0009）与 ADR-0010 纠偏修复。
契约 `2.0.0` 只在 `phase/0` 分支生成，**尚未发布**：未合并 `main`、无 tag、无远程发布、无任何 v2 数据登记。
B3 的剩余遗漏已由 Codex 裁决（D-17 ~ D-25）并写成 ADR-0011 ~ 0017，七份于 2026-09-24 全部 **Accepted**。
**ADR 已获批准，但契约代码尚未改动**：当前的契约缺口在代码批次完成前仍然存在，Phase 0 仍不能认定为“只差宪法批准”。
验收记录见 `docs/reviews/2026-09-23-b1-b2-acceptance.md`；任务方案见 `docs/reviews/2026-09-23-opus-supervision-plan.md`。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | 🔄 进行中 |
| 0.5 | Public Knowledge Base | ⏸️ 未开始 |
| 1 | Market Representation | ⏸️ 未开始 |
| 2 | Market State Engine | ⏸️ 未开始 |
| 3 | Event & Interaction Engine | ⏸️ 未开始 |
| 4 | Outcome Engine | ⏸️ 未开始 |
| 5 | Strategy Library | ⏸️ 未开始 |
| 6 | State × Strategy | ⏸️ 未开始 |
| 7 | Dynamic Discovery | ⏸️ 未开始 |
| 8 | Validation | ⏸️ 未开始 |
| 9 | Synthetic Market Lab | ⏸️ 未开始 |
| 10 | Dynamic Strategy Router | ⏸️ 未开始 |
| 11 | Continuous Research Loop | ⏸️ 未开始 |
| 12 | Strategy Evolution | ⏸️ 未开始 |
| 13 | Production Adaptive System | ⏸️ 未开始 |
| 14 | Technology Migration | ⏸️ 未开始 |

## 3. 已完成

- ✅ 架构蓝图：11 份架构文档、路线图、研究宪法草案
- ✅ 项目记忆体系：`PROJECT_MEMORY.md` + CLAUDE.md 中的读取、更新和恢复规则
- ✅ D-06 已定：Python 3.13 + uv，与系统 Python 隔离（ADR-0003）
- ✅ D-07 已定：本地 Git 仓库，首个 baseline commit 已完成（ADR-0004）
- ✅ D-03 已定：研究 / 生产边界冻结（ADR-0005 Accepted）
- ✅ D-05 已定：策略生命周期 v2 冻结（ADR-0006 Accepted，取代 ADR-0002 第 5 条）
- ✅ D-09 结构已定：三层验证架构 + 两步冻结（ADR-0007 Accepted）
- ✅ 研究宪法重组为纯原则（0.2.0-draft，不含任何数值），待 Phase 0 批准为 1.0.0
- ✅ Validation Profile 与 Experiment Metadata 的概念契约已写入文档；复现元组已含 Profile 版本
- ✅ Phase 0 环境：Python 3.13.15 + uv 虚拟环境（未改系统 Python）
- ✅ Phase 0 代码：领域契约、生命周期状态机、三层验证契约、错误分类、JSON Schema 导出
- ✅ Phase 0 工程基线：`pytest` / `ruff check` / `ruff format --check` / `mypy --strict` 均可运行且全绿
- ✅ C-1、C-2 已决定：先纸面交易再成为生产候选；PAPER = 单策略观察，ACTIVE = 组合正式启用（带模拟 / 实盘标记）
- ✅ D-09 提案：验证门槛三层结构 + BTCUSDT 1H 初始参数建议（未批准）
- ✅ B1（ADR-0008）：14 个映射字段改为只读载荷、逐模型内容哈希排除表、规范化 JSON 约定、v1 固定向量
- ✅ B2（ADR-0009）：完整实验身份与依赖内容绑定、报告绑定运行、契约版本号提升到 2.0.0、v1 只读兼容入口
- ✅ ADR-0010 纠偏：复制更新重新校验、唯一 ASCII SemVer 2.0.0 语法、v1 顶层 shape gate、Schema 表达键值格式
- ✅ B1 / B2 / ADR-0010 已通过 Codex 最终独立复验并正式验收（`docs/reviews/2026-09-23-b1-b2-acceptance.md`）
- ✅ B3 只读审计与技术裁决（D-17 ~ D-25）完成，ADR-0011 ~ 0017 经 Codex 文档复核后全部 Accepted（仅文档，未实现）

## 4. 当前正在做

- ✅ B1（ADR-0008 只读载荷）、B2（ADR-0009 实验身份）、ADR-0010 纠偏全部完成并由 Codex 验收通过
- ✅ B3 的七份 ADR 已起草、复核并 Accepted（只改 Markdown，未动代码、测试、Schema）
- ✅ 复核修正已落实：D-18 编号补齐、ADR-0014 归入 D-20.4、`called_at` 改为显式必填且无默认值
- ✅ ADR-0017 的文档同步已随接受完成（`05-plugin.md`、`02-domain.md`、`core/contracts/README.md`、roadmap）
- 🔄 下一步：按串行批次 0011 → … → 0017 实施 B3 代码（已由 Codex 授权，尚未开始）
- ⏸️ Phase 0 关闭复审（批次 C）、Constitution 批准、main 合并、tag 均未授权

Codex 在 `cd84a4e` 上最终复验的实际结果：
`pytest` 545 passed、`ruff check` All checks passed、`ruff format --check` 94 files already formatted、
`mypy` Success: no issues found in 27 source files。关键探针确认 `model_copy` 后仍为 `FrozenMapping`
且别名隔离，错 kind / 缺依赖 / 空 `run_id` 被拒绝，v1 合法载荷接受、错模型与多余字段拒绝。
Schema：current 36 份（`schema_version` 默认 `2.0.0`）、legacy 35 份（`schemas/v1/`，逐字节不变）。

## 5. 下一步

### 我（Raphael）需要做

- 无阻塞项在等我：ADR-0011 ~ 0017 已由 Codex 依授权接受，下一步是 Claude Code 的串行实现
- B3 实现完成后，再决定研究宪法 `docs/research/constitution.md` 是否批准为 1.0.0
- 决定远程仓库位置（不阻塞 Phase 0，但阻塞 PR / CI）

### Claude Code 需要做

- 已批准且已完成：只读复核独立审查发现、准备并批准 ADR-0008 / 0009 方案 A
- 已批准且已完成：Opus 串行实施 B1、B2；两批各留一个可恢复 commit，未合并 main
- 已批准且已完成：ADR-0010 的 D-13 ~ D-16 纠偏，并通过 Codex 最终复验
- 已批准且已完成：把 Codex 的 D-17 ~ D-25 裁决写成 ADR-0011 ~ 0017，并同步 ADR 索引与项目文档
- 已批准且已完成（本轮）：按 Codex 复核结论修正编号与 `called_at`、把七份 ADR 置为 Accepted、
  同步 ADR-0017 涉及的架构文档（docs-only，未动代码 / 测试 / Schema）
- 已批准、未开始：B3 代码实现，串行批次 0011 → 0012 → 0013 → 0014 → 0015 → 0016 → 0017，每批独立 commit
- 未批准：Constitution 批准、Phase 0 关闭复审（批次 C）、其他 Phase、环境安装、main 合并与 tag

## 6. 当前待决策

**Phase 0 契约修复（已决定并已验收）**
- 问题：独立审查发现不可变对象可被嵌套修改、实验身份未覆盖完整规格，并发现生命周期审批遗漏等问题。
- D-11：[ADR-0008](docs/adr/0008-contract-payload-immutability.md) Accepted，B1 已实施并验收。
- D-12：[ADR-0009](docs/adr/0009-experiment-identity-binding.md) Accepted，B2 已实施并验收。
- D-13 ~ D-16：[ADR-0010](docs/adr/0010-contract-construction-and-canonical-versioning.md) Accepted，纠偏已实施并验收。
- 验收结论见 [B1/B2 验收记录](docs/reviews/2026-09-23-b1-b2-acceptance.md)；未实现 Registry/Runner/存储，未登记任何实验。
- 契约 `2.0.0` 只在 `phase/0` 生成，尚未合并 `main`、无 tag、无远程发布、无 v2 数据登记。

**B3 技术方向（Codex 已裁决并接受，不需要 Raphael 决定）**

D-17 ~ D-25 由 Codex 依授权作出，已写成七份 ADR，2026-09-24 全部 **Accepted**。它们是
**已确定的技术结论**，不是待 Raphael 决策项；Raphael 的批准点仍然只有：Constitution 1.0.0、
main 合并、tag、实盘与环境变更。

| 裁决 | 确定结论 | ADR |
|---|---|---|
| D-17 | 失败的 revalidation 不得自动退役（`REVALIDATION → RETIRED` 需人类批准）；历史主体一致、时间单调；授权有效期与 Risk Gate 时序绑定同一主体；删除自报的 `live_execution_enabled`，实盘开关交未来 Control Plane | [0011](docs/adr/0011-lifecycle-subject-authorization-and-time.md) |
| D-23 | `kind` 冻结为不可覆盖的字面量；Feature / State / Event / Strategy 输入按白名单收紧；Outcome 不得进入输入；lineage 是溯源，不收紧 | [0012](docs/adr/0012-information-flow-and-kind-invariants.md) |
| D-19 | 整体 Verdict 是门结果集合的精确确定性函数；报告外因素必须物化为一个门；`gate_id` 唯一 | [0013](docs/adr/0013-deterministic-verdict-and-finite-numbers.md) |
| D-20 | 全局拒绝 NaN / ±Infinity（不先转 null）；threshold 与来源成对出现；两个概率型阈值的结构范围为 `[0,1]`，**不选任何实际阈值** | [0013](docs/adr/0013-deterministic-verdict-and-finite-numbers.md) |
| D-20.4 | Validation Profile 的普适结构不变量（窗口 / 封存长度 / 压力倍数 / 观察期的符号约束），**不选 Phase 4 数值** | [0014](docs/adr/0014-validation-profile-structural-invariants.md) |
| D-21 | 内容哈希统一为 `ContentHash`；Git OID 独立且必须完整；生产代码身份改为结构化 `GitCodeRevision` | [0015](docs/adr/0015-audit-identity-types-and-version-bindings.md) |
| D-22 | `constitution_version` 用唯一 ASCII SemVer；Profile 绑定改为 `Ref(kind=profile)` + 内容哈希；元数据使用完整 `ProfileSelection`；删除重复的 `profile_version` | [0015](docs/adr/0015-audit-identity-types-and-version-bindings.md) |
| D-18 | `LlmCall` 三项内容引用全部必填（新值对象 `ContentBlobRef`）；`called_at` 显式必填且**无默认值**；可取回性与内容一致性延期，严禁自报 `verified` | [0016](docs/adr/0016-llmcall-content-bindings.md) |
| D-24 | Provider 采用方案 B：Phase 0 只冻结职责与语义，可执行 Protocol / DTO / 契约测试随首次消费它的 Phase 交付并验收 | [0017](docs/adr/0017-provider-delivery-schedule.md) |
| D-25 | 上述收窄仍属**尚未发布**的 `2.0.0`，不升 major（只在 `phase/0`、未合并 main、无 tag / 远程发布 / v2 数据登记）；发布后做同类改变必须升 major | 写入 0011 ~ 0016 各自的版本小节 |

- 串行批次（已由 Codex 授权，尚未开始）：**0011 → 0012 → 0013 → 0014 → 0015 → 0016 → 0017**，
  每批一个独立的可恢复 commit，中途不发布 v2、不登记实验、不合并 main。
- 当前状态：七份 ADR 全部 Accepted，但**代码、测试、Schema 未改动**；契约缺口在代码批次完成前仍然存在。
- 编号已确定：D-17 ~ D-25 连续唯一；ADR-0016 = D-18；ADR-0014 = D-20.4（不是新编号）。

**批准研究宪法 1.0.0**（仍未满足；已不是唯一关闭条件）
- 问题：是否把 `docs/research/constitution.md` 从 0.2.0-draft 批准为 1.0.0。
- 为什么需要决定：验收标准写明"Constitution 状态为 Approved"；这是人工决定，Claude 不能代批。
- 可选方案：批准 / 要求修改后再批准。
- 推荐方案：先处理关闭阻塞，再审阅九章原则；不引入任何数值阈值。

**远程仓库位置**（不阻塞 Phase 0；阻塞 PR / CI 流程）
- 问题：是否在 GitHub 建立远程仓库，私有还是公开，仓库名用什么。
- 为什么需要决定：没有远程就无法使用 PR 与 CI；本地历史也没有异地备份。
- 可选方案：A. GitHub 私有仓库（gh CLI 已登录 raphael2025）；B. 暂不建远程，继续纯本地；C. 其他托管。
- 推荐方案：A，私有。

**不阻塞当前阶段（NOT BLOCKING）：** D-01、D-02、D-08、D-10（Phase 1 前）· D-04 与 D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）· D-09 的 H-3 ~ H-7 · ADR-0005 / 0006 的细节问题 Q-1 ~ Q-7 · 远程仓库与 Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ B1/B2/ADR-0010 已验收，但工程检查与批次验收都不能替代 Phase 0 关闭复审（批次 C 未执行）
- ⚠️ 生命周期审批、subject 校验、授权有效期、信息流白名单、判定函数等遗漏**仍未修复**：
  ADR-0011 ~ 0017 虽已 Accepted，但仍只是文档，契约代码与批准前完全一致
- ⚠️ `core/compat/v1.py` 的 v1 gate 只做**顶层**形状检查，不是完整 JSON Schema 递归校验
- ⚠️ `LlmCall` 仍只存三个哈希，06-experiment.md §2 要求的"完整输入输出"仍是未关闭缺口（ADR-0016 待实施）
- ⚠️ 传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性仍是未实现的 Runner / Registry 义务
- ⚠️ 外部是否存在 v1 历史数据证据不足，因此不宣称迁移路径已在真实数据上验证
- ⚠️ Docker 尚未安装：Phase 1 之后的本地服务依赖它
- ⚠️ 外部数据盘未挂载：`~/BTC` 当前不可访问
- ⚠️ 研究宪法仍是草案（0.2.0-draft）：Phase 0 批准为 1.0.0 前不能判定任何实验
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ WSL 内存约 15 GiB：大规模行情数据需要分批处理

## 8. 当前禁止事项

- ❌ 不开始 Phase 0.5（公开知识库）
- ❌ 不实现 Feature / Strategy / Backtest（属于 Phase 1+）
- ❌ 不安装软件（包括 Python 3.13、Docker），除非获得授权
- ❌ 不修改系统配置、`.wslconfig`、Git 全局配置
- ❌ 不修改、移动或删除旧项目（`/mnt/e/alpha-autoquant`、`/mnt/e/hlens-cryptoplus`）与旧数据
- ❌ 不选择 D-09 的五类数值（Phase 4 校准后才冻结）
- ❌ 不在宪法中写入任何数值阈值
- ❌ 不改变 Domain Contract
- ❌ 不因为回测结果修改研究规则
- ❌ Claude 不替 Raphael 做架构决策

## 9. 最近一次变化

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-09-24 | Codex 完成 B3 文档复核：补齐 D-18、把 ADR-0014 归入 D-20.4、`called_at` 改为显式必填且无默认值；七份 ADR 全部 Accepted，并同步 ADR-0017 涉及的架构文档 | B3 实现已获 Codex 授权但**尚未开始**；本提交仍是 docs-only，契约缺口未修复 |
| 2026-09-24 | Codex 完成 B3 只读审计并裁决 D-17 ~ D-25；Claude Code 据此起草 ADR-0011 ~ 0017，同步 ADR 索引与项目文档 | B3 技术方向已确定但未实施；契约代码未动 |
| 2026-09-24 | Codex 对 `cd84a4e` 最终独立复验：B1、B2、ADR-0010 纠偏正式验收通过；授权 B3 只读规划与 ADR 起草 | `cd84a4e` 成为 last known good；契约 2.0.0 仍只在 `phase/0`，未合并 main / 无 tag / 无远程 / 无数据登记 |
| 2026-09-23 | Codex 验收 B1/B2 后裁决 ADR-0010（D-13~D-16）并实施：复制更新重新校验、唯一 ASCII SemVer 语法、v1 顶层 shape gate、Schema 表达键值格式 | 公开构造路径唯一；版本身份无 Unicode / 前导零歧义；旧载荷不能凭空获得 legacy 身份；契约仍为未发布的 2.0.0 |
| 2026-09-23 | B2 实施完成：完整实验身份与依赖绑定、Report→Run 绑定、契约 2.0.0 发布、v1 只读兼容入口与版本化快照 | 引用不同策略的实验不再哈希碰撞；v1/v2 哈希不可比较，旧记录无自动晋升资格 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：**
1. ⏳ current 36 份 + legacy 35 份 Schema；不可变性与实验身份已修复并验收（B1/B2/ADR-0010），交付范围待关闭复审
2. ⏳ 转移图测试通过；审批与历史归属校验仍有缺口（ADR-0011 已 Accepted，未实施）
3. ✅ 契约层无基础设施依赖（导入检查测试）
4. ✅ 本地测试命令可运行（ADR-0010 后实际 545 项通过）
5. ✅ lint / 类型检查命令可运行（ruff + mypy strict 全绿）
6. ⏳ 研究宪法获批为 1.0.0（**需 Raphael**）

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. 研究宪法 0.2.0-draft 获批为 1.0.0（纯原则，不含数值）
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 现在不需要你决定什么：ADR-0011 ~ 0017 已由 Codex 依授权接受，B3 实现也已由它授权。
2. 想了解 B3 会改什么，读 §6 的裁决表即可（七份 ADR 都在 `docs/adr/`）。
3. 之后依次是：B3 实现 → Phase 0 关闭复审 → 研究宪法 1.0.0 → 合并 main 与 tag（后两项需要你）。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. B1（`4f83e18`）、B2（`4e0f6e3`）、ADR-0010 纠偏（`cd84a4e`）已验收。
2. 已完成：D-17 ~ D-25 的 ADR 起草、复核修正与接受，以及 ADR-0017 的文档同步（全部 docs-only）。
3. 下一步：按 §6 的串行批次实施 ADR-0011 ~ 0017，每批一个独立 commit，提交前实际运行
   `pytest` / `ruff check` / `ruff format --check` / `mypy`。
4. 不批准 Constitution、不关闭 Phase 0、不合并 main、不创建 tag。
5. 不安装软件、不改系统 / Git 配置、不触碰旧项目与外部数据。
