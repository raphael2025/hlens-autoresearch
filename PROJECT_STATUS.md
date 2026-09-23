# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution**（进行中） |
| 当前子阶段 | ADR-0008 / 0009 已获批；Opus 串行实施 B1/B2 |
| 总体状态 | 🔄 进行中 |
| 最后更新时间 | 2026-09-23 |

Phase 0 已有代码和工程检查基线，但独立审查发现不可变性、实验身份和生命周期等问题。当前先准备修复 ADR，不能再认定“只差宪法批准”。任务方案见 `docs/reviews/2026-09-23-opus-supervision-plan.md`。

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
- ✅ Phase 0 代码：领域契约、生命周期状态机、三层验证契约、错误分类、35 份 JSON Schema
- ✅ Phase 0 测试：126 项通过（契约、状态机、架构边界、文档一致性）
- ✅ Phase 0 工程基线：`pytest` 126 通过、`ruff check` 通过、`ruff format` 无差异、`mypy --strict` 无错误
- ✅ C-1、C-2 已决定：先纸面交易再成为生产候选；PAPER = 单策略观察，ACTIVE = 组合正式启用（带模拟 / 实盘标记）
- ✅ D-09 提案：验证门槛三层结构 + BTCUSDT 1H 初始参数建议（未批准）

## 4. 当前正在做

- 🔄 Opus 实施 B1（只读载荷）与 B2（实验身份/契约 2.0.0）；Codex 负责验收与文档

## 5. 下一步

### 我（Raphael）需要做

- 审阅本轮产出的具体修复 ADR 与任务批次；契约方案获批后再实施
- 修复与关闭复审完成后，再决定研究宪法 `docs/research/constitution.md` 是否批准为 1.0.0

### Claude Code 需要做

- 已批准且已完成：只读复核独立审查发现、准备并批准 ADR-0008 / 0009 方案 A
- 已批准：Opus 串行实施 B1、B2；每批更新相关文档并提交 Git，不合并 main
- 未批准：B3、Constitution 批准、其他 Phase、环境安装与 main 合并

## 6. 当前待决策

**Phase 0 修复方案（已决定）**
- 问题：独立审查发现不可变对象可被嵌套修改、实验身份未覆盖完整规格，并发现生命周期审批遗漏等问题。
- 为什么需要决定：修复涉及冻结契约与实验复现机制，必须先有 Proposed ADR，再由 Raphael 批准。
- 当前授权：ADR-0008 / 0009 方案 A 与 B1/B2 实施；中间不发布 v2、不登记实验、不实现 Registry/Runner。
- D-11：[ADR-0008](docs/adr/0008-contract-payload-immutability.md) 已 Accepted。
- D-12：[ADR-0009](docs/adr/0009-experiment-identity-binding.md) 已 Accepted。
- 执行范围见 [Opus 执行交接](docs/reviews/2026-09-23-opus-phase0-handoff.md)；不含 B3、宪法批准或 main 合并。
- Phase 0 Provider 签名交付范围仍需明确：05-plugin.md 的承诺与 roadmap 验收表不一致，暂不自行增加或删除验收条件。

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

- ⚠️ 独立审查发现契约不变量未充分落实；工程检查通过不能替代关闭复审
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
| 2026-09-23 | Raphael 授权 Codex 决定项目技术方向并控制 Claude Code；Codex 批准 ADR-0008/0009 方案 A 与 B1/B2 | Claude Code 开始串行修复契约；Constitution、其他 Phase 和 main 合并仍未授权 |
| 2026-09-23 | 独立审查发现 Phase 0 关闭阻塞；Raphael 指定 Codex 控制与文档、Claude Code Opus 负责后续执行；先准备方案与 Proposed ADR | Phase 0 保持开启，具体代码方案待批准 |
| 2026-09-23 | 确立 Git/GitHub 协作协议（分支模型、合并策略、PR、合并前验证、tag 约定）写入 CLAUDE.md §10 | 工程历史规则明确；远程仓库仍待决定 |
| 2026-09-23 | Phase 0 开启并完成实现：环境、契约、状态机、35 份 Schema、126 项测试通过 | 只差宪法批准即可关闭 Phase 0 |
| 2026-09-23 | ADR-0007 获批：宪法重组为纯原则（0.2.0-draft）、Profile 与 Experiment Metadata 概念契约落地、复现元组加入 Profile 版本 | Phase 0 的架构前置条件全部完成 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：**
1. ⏳ 已有 35 份 Schema；不可变性、实验身份与交付范围待修复复审
2. ⏳ 转移图测试通过；审批与历史归属校验仍有缺口
3. ✅ 契约层无基础设施依赖（导入检查测试）
4. ✅ 本地测试命令可运行（实现阶段记录 126 项通过；独立只读审查实际 124 通过、2 未运行，另做内存 Schema 比较）
5. ✅ lint / 类型检查命令可运行（ruff + mypy strict 全绿）
6. ⏳ 研究宪法获批为 1.0.0（**需 Raphael**）

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. 研究宪法 0.2.0-draft 获批为 1.0.0（纯原则，不含数值）
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 审阅 Opus 复核后整理的 Proposed ADR 和修复批次。
2. 批准具体契约方案后，由 Opus 实施，Codex 复审。
3. 修复和验收完成后，再决定宪法批准、Phase 0 关闭及下一阶段。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 本轮复核与 Proposed ADR 已完成，等待 D-11 / D-12 的具体批准。
2. 获批后由 Opus 串行实施 B1/B2，Codex 只控制、审查和输出文档。
3. 不修改实现、测试、Schema、依赖；不安装软件；不开始 Phase 0.5。
4. 具体 ADR 获批后才接受对应实现任务；由 Codex 汇总文档与验收结论。
