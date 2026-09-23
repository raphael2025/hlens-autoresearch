# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution**（进行中） |
| 当前子阶段 | 契约与状态机实现、测试 |
| 总体状态 | 🔄 进行中 |
| 最后更新时间 | 2026-09-23 |

Phase 0 已开启：环境、契约、状态机、测试正在实现中。

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
- ✅ C-1、C-2 已决定：先纸面交易再成为生产候选；PAPER = 单策略观察，ACTIVE = 组合正式启用（带模拟 / 实盘标记）
- ✅ D-09 提案：验证门槛三层结构 + BTCUSDT 1H 初始参数建议（未批准）

## 4. 当前正在做

- 🔄 等待 Raphael 授权安装 Python 3.13 并说"开启 Phase 0"

## 5. 下一步

### 我（Raphael）需要做

- 授权安装 Python 3.13（uv）
- 说"开启 Phase 0"
- 准备好时授权安装 Python 3.13，并说"开启 Phase 0"

### Claude Code 需要做

- 目前**没有已批准的开发任务**
- 可执行：根据你的决定更新 ADR 状态、同步相关文档、更新本文件与项目记忆

## 6. 当前待决策

**开启 Phase 0**（唯一阻塞）
- 问题：是否授权安装 Python 3.13（uv 管理，不动系统 Python）并开启 Phase 0。
- 为什么需要决定：Phase 0 的架构前置条件已全部满足，只差环境授权与开启指令。
- 可选方案：现在开启 / 先处理 H-3 ~ H-7 等开放问题 / 暂缓。
- 推荐方案：现在开启。H-3 ~ H-7 是 Phase 4 校准前的输入，不阻塞 Phase 0。

**不阻塞当前阶段（NOT BLOCKING）：** D-01、D-02、D-08、D-10（Phase 1 前）· D-04 与 D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）· D-09 的 H-3 ~ H-7 · ADR-0005 / 0006 的细节问题 Q-1 ~ Q-7 · 远程仓库与 Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ Python 3.13 尚未安装：需要在 Phase 0 开启时授权安装
- ⚠️ Docker 尚未安装：Phase 1 之后的本地服务依赖它
- ⚠️ 外部数据盘未挂载：`~/BTC` 当前不可访问
- ⚠️ 研究宪法仍是草案（0.2.0-draft）：Phase 0 批准为 1.0.0 前不能判定任何实验
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ WSL 内存约 15 GiB：大规模行情数据需要分批处理

## 8. 当前禁止事项

- ❌ 不开启 Phase 0，不写业务代码
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
| 2026-09-23 | ADR-0007 获批：宪法重组为纯原则（0.2.0-draft）、Profile 与 Experiment Metadata 概念契约落地、复现元组加入 Profile 版本 | Phase 0 的架构前置条件全部完成 |
| 2026-09-23 | D-09 的 H-1、H-2 获接受：三层验证架构 + 两步冻结 | 原则与数值分离；数值等 Phase 4 校准后再冻结 |
| 2026-09-23 | D-03、D-05 获批：ADR-0005、ADR-0006 Accepted；RETIRED 与 FAILED 的记录位置已分开 | 研究 / 生产边界与生命周期冻结，下一个决策边界是 D-09 |
| 2026-09-21 | C-1、C-2 已决定：先纸面交易再成为生产候选；ACTIVE 带模拟 / 实盘标记，不设独立实盘状态 | 生命周期冲突解决，ADR-0005 / 0006 可以进入批准 |
| 2026-09-21 | D-09 门槛提案完成（三层结构、BTCUSDT 1H 初始建议） | 宪法数字将先校准再冻结 |

## 10. 下一阶段进入条件

**从 Bootstrap 进入 Phase 0，需要同时满足：**
1. ✅ C-1、C-2 已决定
2. ✅ ADR-0005、ADR-0006 已批准
3. ✅ ADR-0007 已批准
4. 授权安装 Python 3.13（uv）
5. Raphael 明确说"开启 Phase 0"

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. 研究宪法 0.2.0-draft 获批为 1.0.0（纯原则，不含数值）
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 授权安装 Python 3.13（uv 管理，不影响系统 Python）。
2. 说"开启 Phase 0"。
3. 之后在 Phase 0 结束时批准研究宪法 1.0.0。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 等待 Raphael 决策；不开启 Phase 0
2. 开启 Phase 0 后：按 roadmap 写 core 契约代码、状态机、Profile 与 Metadata 契约、测试
3. 获授权后：安装 Python 3.13 并建立虚拟环境（Phase 0 开启时）
4. 每次决定后：更新本文件与 `PROJECT_MEMORY.md`，并提交 Git
