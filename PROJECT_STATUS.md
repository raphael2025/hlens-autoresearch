# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution**（进行中） |
| 当前子阶段 | 实现完成，等待你批准研究宪法 1.0.0 |
| 总体状态 | 🔄 进行中 |
| 最后更新时间 | 2026-09-23 |

Phase 0 的代码、测试、lint 与类型检查全部完成；只差你批准研究宪法 1.0.0，Phase 0 才算关闭。

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

- 🔄 等待 Raphael 批准研究宪法 1.0.0（Phase 0 最后一项验收标准）

## 5. 下一步

### 我（Raphael）需要做

- 审阅并批准研究宪法 `docs/research/constitution.md`（0.2.0-draft → 1.0.0）

### Claude Code 需要做

- 目前**没有已批准的开发任务**
- 可执行：根据你的决定更新 ADR 状态、同步相关文档、更新本文件与项目记忆

## 6. 当前待决策

**批准研究宪法 1.0.0**（Phase 0 唯一未满足的验收标准）
- 问题：是否把 `docs/research/constitution.md` 从 0.2.0-draft 批准为 1.0.0。
- 为什么需要决定：验收标准写明"Constitution 状态为 Approved"；这是人工决定，Claude 不能代批。
- 可选方案：批准 / 要求修改后再批准。
- 推荐方案：审阅九章原则后批准；其中不含任何数值。

**不阻塞当前阶段（NOT BLOCKING）：** D-01、D-02、D-08、D-10（Phase 1 前）· D-04 与 D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）· D-09 的 H-3 ~ H-7 · ADR-0005 / 0006 的细节问题 Q-1 ~ Q-7 · 远程仓库与 Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ Python 3.13 尚未安装：需要在 Phase 0 开启时授权安装
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
| 2026-09-23 | Phase 0 开启并完成实现：环境、契约、状态机、35 份 Schema、126 项测试通过 | 只差宪法批准即可关闭 Phase 0 |
| 2026-09-23 | ADR-0007 获批：宪法重组为纯原则（0.2.0-draft）、Profile 与 Experiment Metadata 概念契约落地、复现元组加入 Profile 版本 | Phase 0 的架构前置条件全部完成 |
| 2026-09-23 | D-09 的 H-1、H-2 获接受：三层验证架构 + 两步冻结 | 原则与数值分离；数值等 Phase 4 校准后再冻结 |
| 2026-09-23 | D-03、D-05 获批：ADR-0005、ADR-0006 Accepted；RETIRED 与 FAILED 的记录位置已分开 | 研究 / 生产边界与生命周期冻结，下一个决策边界是 D-09 |
| 2026-09-21 | C-1、C-2 已决定：先纸面交易再成为生产候选；ACTIVE 带模拟 / 实盘标记，不设独立实盘状态 | 生命周期冲突解决，ADR-0005 / 0006 可以进入批准 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：**
1. ✅ 契约与 Schema 导出齐全（35 份）
2. ✅ 状态机只允许定义的转移（测试覆盖）
3. ✅ 契约层无基础设施依赖（导入检查测试）
4. ✅ 本地测试命令可运行（126 项通过）
5. ✅ lint / 类型检查命令可运行（ruff + mypy strict 全绿）
6. ⏳ 研究宪法获批为 1.0.0（**需 Raphael**）

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. 研究宪法 0.2.0-draft 获批为 1.0.0（纯原则，不含数值）
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 审阅 `docs/research/constitution.md`（九章原则，无数值），回复批准或修改意见。
2. 批准后 Phase 0 即可关闭。
3. 之后决定是否开启 Phase 0.5（公开知识库）。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 等待宪法批准；不开始 Phase 0.5
2. 宪法获批后：把 constitution.md 标为 1.0.0 Approved，关闭 Phase 0，更新状态与记忆
3. 获授权后：安装 Python 3.13 并建立虚拟环境（Phase 0 开启时）
4. 每次决定后：更新本文件与 `PROJECT_MEMORY.md`，并提交 Git
