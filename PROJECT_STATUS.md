# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | Phase 0 之前（Architecture Bootstrap 已完成） |
| 当前子阶段 | Phase 0 决策处理：D-06、D-07 已定；D-03、D-05 提案待批；D-09 提案待审 |
| 总体状态 | ⚠️ 等待决策 |
| 最后更新时间 | 2026-09-21 |

项目记忆体系和 Git 仓库已建立；还剩两处规格冲突和两份提案需要你拍板，之后就可以开启 Phase 0。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ⚠️ 等待决策 |
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
- ✅ D-03 提案：研究 / 生产边界设计（ADR-0005，待批）
- ✅ D-05 提案：策略生命周期 v2（ADR-0006，待批）
- ✅ D-09 提案：验证门槛三层结构 + BTCUSDT 1H 初始参数建议（未批准）

## 4. 当前正在做

- 🔄 等待 Raphael 决定冲突 C-1、C-2
- 🔄 等待 Raphael 审阅 ADR-0005、ADR-0006 和 D-09 提案

## 5. 下一步

### 我（Raphael）需要做

- 决定 C-1：先纸面交易还是先成为生产候选
- 决定 C-2：纸面交易（PAPER）和运行中（ACTIVE）的区别
- 批准或修改 ADR-0005、ADR-0006
- 回答 D-09 提案的 H-1、H-2（结构和冻结方式）；H-3 ~ H-7 可以稍后

### Claude Code 需要做

- 目前**没有已批准的开发任务**
- 可执行：根据你的决定更新 ADR 状态、同步相关文档、更新本文件与项目记忆

## 6. 当前待决策

**C-1 纸面交易与生产候选的先后顺序**（阻塞 Phase 0 开启）
- 问题：你的 D-03 链条是"纸面交易 → 生产候选"，D-05 生命周期是"生产候选 → 纸面交易"，两者相反。
- 为什么需要决定：Phase 0 要把生命周期写成代码并冻结。
- 可选方案：A. 生产候选 → 纸面交易（D-05 顺序：研究通过后才进入模拟）；B. 纸面交易 → 生产候选（D-03 顺序：模拟通过后才算候选）。
- 推荐方案：暂不推荐，这是你对"候选"含义的定义问题；两种都可以实现。

**C-2 PAPER 与 ACTIVE 的区别**（阻塞 Phase 0 开启）
- 问题：Phase 13 之前 ACTIVE 也只是模拟交易，那它和 PAPER 有什么不同？
- 为什么需要决定：否则状态机里会有两个含义重复的状态。
- 可选方案：X. PAPER = 单策略观察期，ACTIVE = 进入组合运行，并带"模拟 / 实盘"标记；Y. 增加独立的 LIVE 状态；Z. Phase 13 之前合并两者。
- 推荐方案：X。

**D-03 / D-05 提案批准**（阻塞 Phase 0 开启）
- 问题：是否批准 ADR-0005（研究 / 生产边界）和 ADR-0006（生命周期 v2）。
- 为什么需要决定：它们是 Phase 0 要冻结的契约。
- 可选方案：批准 / 修改后批准 / 退回。其中的细节问题（Q-1 ~ Q-6）大多可以延后，不影响批准。
- 推荐方案：先决定 C-1、C-2，再批准。

**D-09 验证门槛**（阻塞 Phase 0 **完成**，不阻塞开启）
- 问题：是否接受"不可变原则 + 版本化参数 + 实验记录"三层结构，以及"先用临时参数、Phase 4 校准后再冻结"的方式。
- 为什么需要决定：它决定宪法怎么写；没有它，Phase 0 无法完成。
- 可选方案：接受 / 修改 / 退回。
- 推荐方案：接受 H-1、H-2；具体数字等校准后再定。

**不阻塞当前阶段（NOT BLOCKING）：** D-01、D-02、D-08、D-10（Phase 1 前）· D-04（Phase 4 前）· D-09 的 H-3 ~ H-7 · 远程仓库与 Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ 规格冲突 C-1、C-2 未解决：生命周期暂时无法冻结
- ⚠️ Python 3.13 尚未安装：需要在 Phase 0 开启时授权安装
- ⚠️ Docker 尚未安装：Phase 1 之后的本地服务依赖它
- ⚠️ 外部数据盘未挂载：`~/BTC` 当前不可访问
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ WSL 内存约 15 GiB：大规模行情数据需要分批处理

## 8. 当前禁止事项

- ❌ 不开启 Phase 0，不写业务代码
- ❌ 不安装软件（包括 Python 3.13、Docker），除非获得授权
- ❌ 不修改系统配置、`.wslconfig`、Git 全局配置
- ❌ 不修改、移动或删除旧项目（`/mnt/e/alpha-autoquant`、`/mnt/e/hlens-cryptoplus`）与旧数据
- ❌ 不修改正式研究宪法，不批准 D-09
- ❌ 不改变 Domain Contract
- ❌ 不因为回测结果修改研究规则
- ❌ Claude 不替 Raphael 做架构决策

## 9. 最近一次变化

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-09-21 | 采纳 AI 协作协议（写入 CLAUDE.md：HANDOFF、Decision Packet、指令短语） | 你可以用"同意推荐方案 / 选择 A / 暂缓 / 继续分析 / 开启 Phase X"直接下达决定 |
| 2026-09-21 | D-09 门槛提案完成（三层结构、BTCUSDT 1H 初始建议） | 宪法数字将先校准再冻结 |
| 2026-09-21 | 起草 ADR-0005（研究 / 生产边界）和 ADR-0006（生命周期 v2），并发现冲突 C-1、C-2 | 需要你先定顺序和状态含义 |
| 2026-09-21 | ADR-0003（Python 3.13 + uv）、ADR-0004（Git）生效；Git 仓库建立 | 实验可以记录代码版本 |
| 2026-09-21 | 建立项目记忆体系（PROJECT_MEMORY.md、恢复机制） | 跨会话不再依赖聊天记录 |

## 10. 下一阶段进入条件

**从 Bootstrap 进入 Phase 0，需要同时满足：**
1. C-1、C-2 已决定
2. ADR-0005、ADR-0006 已批准
3. 授权安装 Python 3.13（uv）
4. Raphael 明确说"开启 Phase 0"

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. 研究宪法按 D-09 定下的结构获批
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 回答 C-1（先纸面交易还是先生产候选）和 C-2（推荐选 X）。
2. 看 ADR-0005、ADR-0006 的决策表，回复"批准"或要修改的地方。
3. 读 D-09 提案开头的一页摘要（§0），回答 H-1、H-2。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 等待 Raphael 决策；不开启 Phase 0
2. Raphael 决定后：更新 ADR-0005、ADR-0006 状态，同步 `07-validation.md` 等相关文档
3. Raphael 回答 H-1、H-2 后：起草宪法结构调整的 ADR（Proposed）
4. 每次决定后：更新本文件与 `PROJECT_MEMORY.md`，并提交 Git
