# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24），**未验收** |
| 当前子阶段 | 剩余四项主线代码缺口（E1 archive / Raw window 复用、DQ-10 v2 重放、P7 横截面执行接线）已收口；全仓门禁首次全绿（2026-10-01）。下一关键路径：在主线代码上测 E1-CAP-1。 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`） |
| 代码基线 | `phase/1` 经 PR #20、#21 整合入 `main`（2026-10-01）；PR #17–#19 此前已合入 |
| 契约 | 2.5.0；current Schema 148 份 |
| 验证状态 | **全仓门禁通过**（2026-10-01，整合前 `phase/1` 工作树）：pytest 9313 passed / 144 skipped / 0 failed；PostgreSQL 启用的数据库用例另行实跑并修复；ruff、format、mypy 全部通过。详见 [W1 门禁修复记录](docs/reviews/2026-10-01-w1-gate-repair.md)。E1-CAP-1 容量与各 Phase 验收仍未完成。 |
| 最后更新时间 | 2026-10-01 |

Phase 0 已于 2026-09-24 关闭：研究宪法发布为 `1.0.0 / Approved`（ADR-0020），契约 2.0.0 随之发布，此后破坏性契约变化必须升 major 并走 ADR。

远程为私有 GitHub 仓库 `raphael2025/hlens-autoresearch`（ADR-0025），CI 未配置。2026-10-01 核对时 PR #17–#19 均为 MERGED（#19 非 Draft）、无开放 PR；远端有 `main` 与已合并 PR #19 源分支 `claude/modest-bardeen-zbu3vk`。此前 17 个 Codex 临时分支 tip 已归档并清理。本地快照为 13 个分支 / 13 个 worktree；4 个 Claude worktree 有未提交改动，属于进行中，不算主线完成。计数为本轮文档核对快照。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成（tag `phase-0-complete`） |
| 0.5 | Public Knowledge Base | 🧱 检索、审阅写入、标签 / 资产检索已实现；种子的标签 / 资产仍需具名人工审阅；未验收 |
| 1 | Market Representation | 🔄 采集 → 原始 → 规范 → 时点选择 → 质量 → 数据集链路已实现，D3E 已验收、D4 已关闭；归档与 Raw 窗口重复读取已消除（2026-10-01）；上市政策 1.1.0 已写入 BTCUSDT / ETHUSDT 下界 2017-08-17；**E1-CAP-1 未判定，阻断验收** |
| 2 | Market State Engine | 🧱 框架与持久化已实现；未验收 |
| 3 | Event & Interaction Engine | 🧱 已实现；真实 Catalog 尚未建表；未验收 |
| 4 | Outcome Engine + 最小验证门 | 🧱 已实现；Profile 数值未冻结；未验收 |
| 5 | Strategy Library + 回测 | 🧱 已实现；晋升链今天拒绝所有策略，无策略晋升 |
| 6 | State × Strategy | 🧱 已实现；未验收 |
| 7 | Dynamic Discovery | 🧱 六类组合算子语义已接受；时间事件规则已细化（ADR-0100 修订 1）；执行开关默认关闭。横截面 `rank_cs` / `quantile_cs` 已可在显式 pinned universe 下编译为专用根 Provider（2026-10-01 收口），不接单序列组合器或 Research Loop |
| 8 | Validation & Robustness | 🧱 已实现；未验收 |
| 9 | Synthetic Market Lab | 🧱 已实现；只出证据、不选数值；未验收 |
| 10 | Dynamic Strategy Router（纸面） | 🧱 已实现（仅纸面）；未验收 |
| 11 | Continuous Research Loop | 🧱 权威生命周期登记处、钉定数据源、监控指标闭集 2.0.0 与默认运行环境已写；PR #19 增加只读证据强度 / 提案审计视图；G5 与仅报告类指标仍拒绝；真实运行需要部署设置；未测试 |
| 12 | Strategy Evolution | 🧱 合并规则失败即拒；循环内替换提案为可选触发（默认关闭），PR #19 增加只读审计视图；进入 PAPER 仍须人工批准 |
| 13 | Production Adaptive System（仅模拟） | 🧱 已实现；实盘在结构上被拒绝 |
| 14 | Technology Migration | 🧱 框架已实现；尚无具体迁移目标 |
| apps | api / worker / web | 🧱 只读 API、worker、Web 页面已实现；未做浏览器验收 |

图例：🧱 = 代码已写、未验收；阈值 / Profile 数值一律未定；实盘相关一律不实现。

## 3. 已完成

- ✅ 架构蓝图、路线图与 ADR 体系（ADR-0001 起，现至 ADR-0100）
- ✅ 工程基线：Python 3.13 + uv、Git 与私有 GitHub 远程
- ✅ Phase 0：契约、状态机、三层验证架构、研究宪法 1.0.0 发布，tag `phase-0-complete`
- ✅ Phase 1 架构决策（ADR-0021 ~ 0024）与数据基础设施：本地存储、Iceberg Catalog、生产表、归档采集、严格解析、修订存储、REST 补尾（D0 ~ D3E 均已独立验收）
- ✅ 全阶段框架代码（Phase 0.5、2 ~ 14、研究控制台）
- ✅ 2026-09-30 第一轮补全：整合线、ADR-0098（P11 权威）/ ADR-0099（P7 时间序列排名 / 分位）、深度审查修复，经 PR #17 合入 `main`（未测试）
- ✅ 2026-10-01 第二轮补全：ADR-0100 批次与审查修复，经 PR #18 合入 `main`（未测试）
- ✅ 2026-10-01 PR #19 合入 P11 权威证据与 P12 提案审计只读视图（改动含测试文件；最新主线未统一运行门禁）

## 4. 当前正在做

- ✅ 四项主线代码缺口全部收口（2026-10-01）：E1 归档 spool 复用（加固资源释放）、E1 Raw 窗口复用（新实现）、DQ-10 v2 重放（加固防止写出新 v2 manifest）、P7 横截面执行接线（加固拒绝直接引用）。
- ✅ W1 全仓门禁首次全绿：修复首测暴露的约 250 个陈旧测试 / 夹具与 1 个真实缺陷（研究循环哈希记录含 float）；未删除断言、未放宽容差、未改动任何固定哈希值，逐项依据见 [W1 门禁修复记录](docs/reviews/2026-10-01-w1-gate-repair.md)。
- ✅ E1-CAP-1 正式探针（`main@5c3b516`）暴露 replay / resume 的证明调查逐窗口两次 Canonical 扫描、每次遍历全部 manifest，耗时随 N 二次增长；新增并收口 E1-CANONICAL-WINDOW-REUSE（40k replay 198 s → 48 s），RSS 增长不变或略降。
- ⏭️ 在新主线上重跑 E1-CAP-1 正式矩阵（32 MiB 门槛不变）；逐提交 read-back 仍遍历全部 manifest，写入路径时间仍超线性，若要增量化须先立 ADR。

## 5. 下一步

### 我（Raphael）需要做

- 继续逐模块调试；定向测试通过不替代全仓门禁或 Phase 验收
- 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，以消除授权冲突（见 §6 第一项）
- 知识条目标签 / 资产的具名人工审阅；Profile 数值、交易 / 风险预算与实盘保持冻结 / 关闭

### Claude Code 需要做

- 在与 `main` 一致的生产代码上运行 E1-CAP-1 正式矩阵并如实记录结论；32 MiB 门槛不变
- 逐 Phase 验收仍是独立工作；全仓门禁通过不等于 Phase 验收

## 6. 当前待决策

**D-AUTH-CONFLICT（开放，最高优先）**：CLAUDE.md §0 同时包含 2026-09-28 Claude Code PM 授权与 2026-09-30 早段 Codex PM 段落（`fc9f643`），互相冲突。Raphael 于 2026-09-30 在 Claude Code 主会话中指令：「现在你接管这个项目」「给你最大权限 能改所有的代码 文件内容」「你不管文档怎么说 能写的都写了」「你自己决定一切 我不知道也不懂」。本轮按该指令由 Claude Code 执行；CLAUDE.md 自身的修改被环境安全检查拦截（不得自我修改授权文件），留待 Raphael 本地删除 §0 中 Codex 段落以消除冲突。

**开放的阻塞 / 待办**

| ID | 事项 | 状态 |
|---|---|---|
| E1-CAP-1 | Phase 1 完整进程 32 MiB 容量门 | main 正式矩阵已启动但中断，尚无容量判定；阻断 Phase 1 验收 |
| W1 | 全仓测试门禁 | ✅ 2026-10-01 全绿（9313 passed / 144 skipped；PostgreSQL 用例另行实跑）；之后每次合并须复跑 |
| P11-RUN | P11 真实运行 | 需要部署设置 |
| P0.5-REVIEW | 知识库种子标签 / 资产 | 需具名人工审阅 |
| P14-TARGET | 技术迁移 | 无具体迁移目标 |
| D-CATALOG-TABLES | 真实 Catalog 建 `event.*` / `state.*` 表 | 已授权，尚未执行 |
| D-09 | Profile 数值（TBD-1 ~ TBD-5） | 未冻结，Phase 4 校准后决定 |
| D-P11-WINDOW | 滚动循环与固定日历 Profile 如何配合 | ADR-0049 遗留，开放 |

**已决定（仅列索引，详见 ADR 与 Git 历史）**：D-P11-AUTH → ADR-0098；D-P7-OPS → ADR-0088 / 0099 / 0100；P12-LOOP → ADR-0100 §7（取代原暂缓决定）；D-LIST → ADR-0051（政策 1.1.0）；D-HIST → ADR-0032；D-QGAP → ADR-0031；D-33 → ADR-0027；D-FLOAT / D-PFIELDS / D-CTRL → ADR-0052；D-VFAIL → ADR-0053；D-PARTIAL → ADR-0054；E1 各子决定 → ADR-0075 / 0077 / 0093 / 0094 / 0097；D-STATE-INC 暂缓（ADR-0035）。

## 7. 当前风险

- ⚠️ 全仓门禁已通过，但测试通过不等于 Phase 验收；E1-CAP-1 与各 Phase 验收矩阵仍未完成
- ⚠️ 授权文件内部冲突（§6 第一项）未消除前，后续代理可能读到相互矛盾的指挥关系
- ⚠️ E1-CAP-1 正式矩阵已部分运行后中断：成交数据整天规模的内存上界仍未证明
- ⚠️ 官方资料不能证明历史行情的公开时刻：不绑定假设政策时，早于本机采集的历史不可用于回测
- ⚠️ 上市政策 1.1.0 只是研究假设，不能证明真实上市史或排除幸存者偏差
- ⚠️ 契约层只校验结构与声明：传递依赖闭包、`LlmCall` 调用登记完整性、注册存在性、哈希与真实内容一致、泄漏检测、Profile 已冻结等仍依赖运行时服务
- ⚠️ 新运行能力（P7 执行、P12 循环内提案）默认关闭；验收前不得打开
- ⚠️ 生命周期可记录 LIVE 证据，但运行时拒绝 LIVE；没有下单接口或凭据
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史样本外区间在认知上不完全干净
- ⚠️ CI 未配置；本地 warehouse 无异地副本；Docker 未安装、外部数据盘未挂载、WSL 内存约 15 GiB

## 8. 当前禁止事项

- ❌ 不进行任何实盘操作：不下单、不连接实盘账户、不使用交易凭据（须 Raphael 亲自批准）
- ❌ 不为提高回测表现修改验证规则、成本、切分、指标或 Profile 选择（H3）；不削弱测试（H4）；不删除失败实验或生命周期历史（H6）
- ❌ 不猜测或冻结 Profile 数值；不在宪法中写入数值阈值；不修改宪法原则
- ❌ 研究代码不直接晋升为生产代码
- ❌ 不在验收前打开 P7 执行或 P12 循环内提案开关
- ❌ 不替人工审阅者给知识种子写标签 / 资产
- ❌ 不修改、移动或删除旧项目（`/mnt/e/alpha-autoquant`、`/mnt/e/hlens-cryptoplus`）与旧数据
- ❌ 不 force push、不改写已发布历史、不修改全局 Git 配置

## 9. 最近一次变化

> 只保留最近 5 条；更早记录见 Git 历史与 `docs/reviews/`。

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-10-01 | E1 Canonical 窗口复用 | replay / resume 调查不再逐窗口扫 Canonical（每单元一次 block spool + 一次 revision 索引）；修复 E1-CAP-1 测量中暴露的二次耗时 |
| 2026-10-01 | phase/1 → main 整合（PR #20） | 四项代码缺口收口与 W1 修复进入主线 |
| 2026-10-01 | W1 全仓门禁全绿 | pytest 9313 passed / 144 skipped / 0 failed，PostgreSQL 用例另行实跑；ruff、format、mypy 通过；修复研究循环哈希记录含 float 的真实缺陷 |
| 2026-10-01 | 候选加固（`63d09a4`） | 非 frozen pin 及时释放 archive spool；v2 重放漂移不再写出新 manifest；直接引用横截面特征被编译期拒绝 |
| 2026-10-01 | E1 Raw 窗口复用（`fe842b3`） | 每单元 Raw 至多一次窄读 + 一次全量 spool，不再逐窗口重扫；容量另测 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件：全部满足**（2026-09-24）——宪法 1.0.0 Approved 且无数值阈值；Profile 与实验元数据契约已定义；核心实体契约与 Schema 导出（收口时 38 份，现为 148 份，契约 2.5.0）；状态机只允许定义的转移；契约层无基础设施依赖；本地测试命令可运行。

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #21 全部满足。当前 #1 ~ #12 与 #17 满足；全仓门禁已于 2026-10-01 通过；E1-CAP-1 容量门未完成。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 全仓门禁已通过（2026-10-01）；Phase 验收完成前，不把模块视为已验收。
2. 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，消除授权冲突。
3. 系统没有下单能力，没有策略被验证或晋升，Profile 数值未冻结——这些保持不变。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 在与 `main` 一致的提交上运行 E1-CAP-1 正式矩阵（N=10k/100k/500k，3 repeats），如实记录 PASS / FAIL / 中断；32 MiB 门槛不变。
2. 按 roadmap 逐 Phase 准备验收证据；全仓门禁通过不等于验收，每次合并前复跑门禁。
3. 未合入 worktree 候选（P2 / P10 / P11 / P14 等）仍按模块计划逐项审阅，不整支合并、不把 branch-only ADR 当授权。
4. 真实 Catalog 建表仍是独立运行操作；不得实盘、不猜 Profile 数值、不打开 P7 执行或 P12 循环内提案开关。
