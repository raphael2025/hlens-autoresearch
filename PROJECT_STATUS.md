# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24），**未验收** |
| 当前子阶段 | ADR-0100 底层代码批次已随 PR #18 合入；PR #19 的 P11/P12 只读审计视图已合入。当前按最新代码缺口计划推进候选复核与 E1 调试；分支候选均不计为主线完成。 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`） |
| 代码基线 | `main` = `b5f80fe`（PR #19，2026-10-01 合并）；PR #17–#19 已进入主线。工作分支上的改动不计为主线完成 |
| 契约 | 2.5.0；current Schema 148 份 |
| 验证状态 | 最新主线未运行全仓门禁。既有候选结果：Phase 1 infrastructure 2298 passed / 88 skipped；DQ-10 专项 35 passed；P7-CS-EXEC 专项 105 passed；E1 定向集 216 passed。候选 full-shape probe 的 100k `verify_archive` 为 78.207 秒；500k 同阶段运行超过 15 分钟、读取约 17.7 GB 后中断，未形成容量结论。main 正式探针于 100k `verify_archive` 后中断，既非 PASS 亦非 FAIL。上述均不代表主线门禁或 Phase 验收。 |
| 最后更新时间 | 2026-10-01 |

Phase 0 已于 2026-09-24 关闭：研究宪法发布为 `1.0.0 / Approved`（ADR-0020），契约 2.0.0 随之发布，此后破坏性契约变化必须升 major 并走 ADR。

远程为私有 GitHub 仓库 `raphael2025/hlens-autoresearch`（ADR-0025），CI 未配置。2026-10-01 核对时 PR #17–#19 均为 MERGED（#19 非 Draft）、无开放 PR；远端有 `main` 与已合并 PR #19 源分支 `claude/modest-bardeen-zbu3vk`。此前 17 个 Codex 临时分支 tip 已归档并清理。本地快照为 13 个分支 / 13 个 worktree；4 个 Claude worktree 有未提交改动，属于进行中，不算主线完成。计数为本轮文档核对快照。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成（tag `phase-0-complete`） |
| 0.5 | Public Knowledge Base | 🧱 检索、审阅写入、标签 / 资产检索已实现；种子的标签 / 资产仍需具名人工审阅；未验收 |
| 1 | Market Representation | 🔄 采集 → 原始 → 规范 → 时点选择 → 质量 → 数据集链路已实现，D3E 已验收、D4 已关闭；内存有界化已做两轮，正式容量矩阵中断、无结论；上市政策 1.1.0 已写入 BTCUSDT / ETHUSDT 下界 2017-08-17（2026-09-30 联网核实）；**E1-CAP-1 未判定，阻断验收** |
| 2 | Market State Engine | 🧱 框架与持久化已实现；未验收 |
| 3 | Event & Interaction Engine | 🧱 已实现；真实 Catalog 尚未建表；未验收 |
| 4 | Outcome Engine + 最小验证门 | 🧱 已实现；Profile 数值未冻结；未验收 |
| 5 | Strategy Library + 回测 | 🧱 已实现；晋升链今天拒绝所有策略，无策略晋升 |
| 6 | State × Strategy | 🧱 已实现；未验收 |
| 7 | Dynamic Discovery | 🧱 六类组合算子语义已接受；时间事件规则已细化（ADR-0100 修订 1）；执行开关默认关闭。`main@b5f80fe` 对横截面 Provider 的 compiler 接线仍缺；`phase/1` 有专用根 Provider 候选（专项 105 passed），尚未合入 |
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

- 🔎 按[剩余代码计划](docs/plans/2026-09-28-remaining-code-gaps.md)推进四项主线缺口：E1 archive spool reuse、E1 Raw window reuse、P1 DQ-10 v2 重放、P7-CS-EXEC。archive reuse、DQ-10 与 P7 候选在 `phase/1`；Raw window reuse 尚待实现。所有候选均不算主线完成。
- ✅ `phase/1` 的 Phase 1 infrastructure 选择集复跑为 2298 passed / 88 skipped；此前 11 个失败已逐项分诊并通过该选择集复验。此结果只覆盖所列 infrastructure 测试，不代表 `main` 全仓门禁或 Phase 验收。
- ✅ DQ-10 public replay 与 v2 golden compatibility 定向集 `35 passed`；候选修复仍未合入主线。
- ⚠️ 受影响策略回归为 257 passed / 1 skipped / 1 failed；失败的旧 B67 dataset-report hash 在改动前提交 `47446f4` 上精确复现，按既有证据记录，不更新 golden、不归因于本轮变更。
- E1-CAP-1 未得出结论：main 正式探针完成 10k 六个 stage 与 100k `verify_archive` 后因耗时中断；候选 archive-spool full-shape probe 的 500k `verify_archive` 超过 15 分钟、读取约 17.7 GB 后停止，提示逐窗口 Raw scan 为显著成本候选；完整 N/repeat 矩阵均未完成。DQ-9 未定。中断记录见[探针记录](docs/reviews/2026-10-01-e1-cap1-main-partial.md)；代码任务见[剩余代码计划](docs/plans/2026-09-28-remaining-code-gaps.md)与[模块计划](docs/plans/2026-09-28-module-foundation-completion.md)。

## 5. 下一步

### 我（Raphael）需要做

- 继续逐模块调试；定向测试通过不替代全仓门禁或 Phase 验收
- 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，以消除授权冲突（见 §6 第一项）
- 知识条目标签 / 资产的具名人工审阅；Profile 数值、交易 / 风险预算与实盘保持冻结 / 关闭

### Claude Code 需要做

- 完成本地 `phase/1` P7 编译接线与定向调试；后续按计划继续逐模块推进
- 逐项记录检查范围与结果；定向测试或静态检查不等于全仓门禁或 Phase 验收
- 在生产路径与 `main` 一致的提交上测量 E1-CAP-1；32 MiB 门槛不变

## 6. 当前待决策

**D-AUTH-CONFLICT（开放，最高优先）**：CLAUDE.md §0 同时包含 2026-09-28 Claude Code PM 授权与 2026-09-30 早段 Codex PM 段落（`fc9f643`），互相冲突。Raphael 于 2026-09-30 在 Claude Code 主会话中指令：「现在你接管这个项目」「给你最大权限 能改所有的代码 文件内容」「你不管文档怎么说 能写的都写了」「你自己决定一切 我不知道也不懂」。本轮按该指令由 Claude Code 执行；CLAUDE.md 自身的修改被环境安全检查拦截（不得自我修改授权文件），留待 Raphael 本地删除 §0 中 Codex 段落以消除冲突。

**开放的阻塞 / 待办**

| ID | 事项 | 状态 |
|---|---|---|
| E1-CAP-1 | Phase 1 完整进程 32 MiB 容量门 | main 正式矩阵已启动但中断，尚无容量判定；阻断 Phase 1 验收 |
| W1 | 全仓测试门禁 | 从未在当前代码上运行 |
| P1-DQ10 | v2 Dataset 历史 manifest 重放定位 | 候选修复与专项结果 35 passed；仍未合入 `main`，须独立复核 |
| P7-CS | 横截面排名 / 分位的执行接线（ADR-0100） | `phase/1` 有候选实现、专项 105 passed；仍未合入 `main`，不接单序列组合器或 Research Loop |
| E1-RAW | E1 Raw window 重复扫描 | main 每个 proof / canonical window 都重新扫描 Raw catalog；列入剩余代码计划的独立串行任务 |
| P11-RUN | P11 真实运行 | 需要部署设置 |
| P0.5-REVIEW | 知识库种子标签 / 资产 | 需具名人工审阅 |
| P14-TARGET | 技术迁移 | 无具体迁移目标 |
| D-CATALOG-TABLES | 真实 Catalog 建 `event.*` / `state.*` 表 | 已授权，尚未执行 |
| D-09 | Profile 数值（TBD-1 ~ TBD-5） | 未冻结，Phase 4 校准后决定 |
| D-P11-WINDOW | 滚动循环与固定日历 Profile 如何配合 | ADR-0049 遗留，开放 |

**已决定（仅列索引，详见 ADR 与 Git 历史）**：D-P11-AUTH → ADR-0098；D-P7-OPS → ADR-0088 / 0099 / 0100；P12-LOOP → ADR-0100 §7（取代原暂缓决定）；D-LIST → ADR-0051（政策 1.1.0）；D-HIST → ADR-0032；D-QGAP → ADR-0031；D-33 → ADR-0027；D-FLOAT / D-PFIELDS / D-CTRL → ADR-0052；D-VFAIL → ADR-0053；D-PARTIAL → ADR-0054；E1 各子决定 → ADR-0075 / 0077 / 0093 / 0094 / 0097；D-STATE-INC 暂缓（ADR-0035）。

## 7. 当前风险

- ⚠️ 两轮大规模代码补全均未测试：合并进 `main` 不等于验证通过，首次全仓门禁可能暴露大量失败
- ⚠️ 授权文件内部冲突（§6 第一项）未消除前，后续代理可能读到相互矛盾的指挥关系
- ⚠️ E1-CAP-1 正式矩阵已部分运行后中断：成交数据整天规模的内存上界仍未证明
- ⚠️ 官方资料不能证明历史行情的公开时刻：不绑定假设政策时，早于本机采集的历史不可用于回测
- ⚠️ 上市政策 1.1.0 只是研究假设，不能证明真实上市史或排除幸存者偏差
- ⚠️ 契约层只校验结构与声明：注册存在性、哈希与真实内容一致、泄漏检测、Profile 已冻结等仍依赖运行时服务
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
| 2026-10-01 | `phase/1@48c7d01` E1/Dataset 调试 | 修复 DQ-10 下 v2 manifest 与 selection snapshot 的关联查找；修正质量 prefix 节点上界；补齐隔离 Dataset fixture 前置报告并缩小 smoke 数据规模。定向 101 passed / 3 skipped，子进程 smoke 1 passed，Ruff / mypy 通过；未合入，E1-CAP-1 未测 |
| 2026-10-01 | P7-CS-EXEC 定向调试 | 初次收集发现 allowlist Provider 缺 `plugin_key()`；补齐声明并消除 mypy 变量名冲突。100 项定向测试、Ruff、mypy 通过；未合入、未开启执行 |
| 2026-10-01 | E1-ARCHIVE-REUSE 候选调试 | verifier 最多保留一个严格解析 archive spool 跨窗口复用；parser 临时文件指向 canonical scratch；定向集 216 passed，1k/2k/3k 六阶段诊断完成但不满足正式协议。候选留在 `phase/1`，未进入主线；容量未验证 |
| 2026-10-01 | main E1-CAP-1 正式探针中断 | clean `main@b5f80fe` 完成 10k 全部 stage 与 100k verify_archive 后因耗时中断；状态 interrupted，不能判定整体 PASS 或 FAIL。细节与样本见 review artifact |
| 2026-10-01 | 计划与状态按 main / 远端重对账 | 确认 PR #17–#19 均已合并、无开放 PR；更新主线缺口队列，并将候选 500k Raw 重扫中断记为诊断信号，不作容量判定 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件：全部满足**（2026-09-24）——宪法 1.0.0 Approved 且无数值阈值；Profile 与实验元数据契约已定义；核心实体契约与 Schema 导出（收口时 38 份，现为 148 份，契约 2.5.0）；状态机只允许定义的转移；契约层无基础设施依赖；本地测试命令可运行。

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #21 全部满足。当前 #1 ~ #12 与 #17 满足；E1-CAP-1 容量门与全仓门禁未完成。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 继续按依赖顺序逐模块调试；在全仓门禁与 Phase 验收完成前，不把模块视为已验收。
2. 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，消除授权冲突。
3. 系统没有下单能力，没有策略被验证或晋升，Profile 数值未冻结——这些保持不变。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 按[剩余代码计划](docs/plans/2026-09-28-remaining-code-gaps.md)复核候选分支并串行处理四项主线代码缺口；未合入内容继续标为进行中。
2. 对照模块计划拆分 P2 / P11 共享改动与 branch-only ADR；不把未合入内容记作主线完成。
3. 每个模块的定向结果单独记录；后续仍需全仓门禁与逐 Phase 验收。
4. 在生产路径与 `main` 一致的提交上测量 E1-CAP-1；32 MiB 门槛不变。
5. 真实 Catalog 建表虽已授权，仍作为独立运行操作；不得实盘、不猜 Profile 数值、不把代码合并称为 Phase 验收。
