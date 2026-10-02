# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24），**未验收** |
| 当前子阶段 | 第三轮代码收口完成（2026-10-02）：ADR-0101 ~ 0106、0108 ~ 0110 全部实现并整合到 `phase/1@1536a409`，无剩余代码缺口。下一关键路径：PostgreSQL 用例实跑 → 合入 `main` → 在 `main` 上重跑 E1-CAP-1（含预填充历史场景）。 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`） |
| 代码基线 | `phase/1@1536a409`（本轮整合，待经 PR 合入 `main`）；`main@d9ddd67`（PR #22） |
| 契约 | **2.6.0**（ADR-0109）；current Schema 148 份 |
| 验证状态 | **全仓门禁通过**（2026-10-02，`phase/1@1536a409`，分四段运行）：pytest 10570 passed / 148 skipped / 0 failed（2 项因 `/tmp` tmpfs 写满失败，单独重跑通过）；ruff、format、mypy（884 文件）全部通过。PostgreSQL 启用用例实跑中。E1-CAP-1 容量与各 Phase 验收仍未完成。 |
| 最后更新时间 | 2026-10-02 |

Phase 0 已于 2026-09-24 关闭：研究宪法发布为 `1.0.0 / Approved`（ADR-0020），契约 2.0.0 随之发布，此后破坏性契约变化必须升 major 并走 ADR。

远程为私有 GitHub 仓库 `raphael2025/hlens-autoresearch`（ADR-0025），CI 未配置。2026-10-02 清理后本地分支只有 `main` 与 `phase/1`；2026-10-01 分支轮的 11 个候选分支 / worktree 已核对（工作已在 `phase/1` 以 Accepted ADR 重新落地，或属 Rejected 的 ADR-0107），tip 与未提交内容归档于 `refs/archive/2026-10-02/` 后删除。另有门禁与探针用的分离 worktree。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成（tag `phase-0-complete`） |
| 0.5 | Public Knowledge Base | 🧱 检索、审阅写入、标签 / 资产检索已实现；种子的标签 / 资产仍需具名人工审阅；未验收 |
| 1 | Market Representation | 🔄 全链路已实现；单元级提交（ADR-0108）与 v3 生产入口（ADR-0101 / 0109）已实现；**E1-CAP-1 待在 `main` 上重跑，阻断验收** |
| 2 | Market State Engine | 🧱 框架与持久化已实现；未验收 |
| 3 | Event & Interaction Engine | 🧱 已实现；真实 Catalog 尚未建表；未验收 |
| 4 | Outcome Engine + 最小验证门 | 🧱 已实现；Profile 数值未冻结；未验收 |
| 5 | Strategy Library + 回测 | 🧱 已实现；晋升链今天拒绝所有策略，无策略晋升 |
| 6 | State × Strategy | 🧱 已实现；未验收 |
| 7 | Dynamic Discovery | 🧱 计划绑定、准入交接、拒绝审计（ADR-0103）与重启恢复（ADR-0110）已实现；执行开关默认关闭；未验收 |
| 8 | Validation & Robustness | 🧱 已实现；未验收 |
| 9 | Synthetic Market Lab | 🧱 已实现；只出证据、不选数值；未验收 |
| 10 | Dynamic Strategy Router（纸面） | 🧱 已实现（仅纸面）；未验收 |
| 11 | Continuous Research Loop | 🧱 运维收口（ADR-0105）：Lifecycle 写入 CLI、基线导出、批量驱动、Dataset 版 operator 与 resolver / 环境测试；真实运行需要部署设置与冻结 Profile；未验收 |
| 12 | Strategy Evolution | 🧱 合并规则失败即拒；循环内替换提案为可选触发（默认关闭），PR #19 增加只读审计视图；进入 PAPER 仍须人工批准 |
| 13 | Production Adaptive System（仅模拟） | 🧱 已实现；实盘在结构上被拒绝 |
| 14 | Technology Migration | 🧱 迁移目标 = 独立参考回测引擎（ADR-0106），已实现；未验收 |
| apps | api / worker / web | 🧱 只读 API、worker、Web 页面已实现；未做浏览器验收 |

图例：🧱 = 代码已写、未验收；阈值 / Profile 数值一律未定；实盘相关一律不实现。

## 3. 已完成

- ✅ 架构蓝图、路线图与 ADR 体系（ADR-0001 起，主线现至 ADR-0108；0101–0107 仅在分支）
- ✅ 工程基线：Python 3.13 + uv、Git 与私有 GitHub 远程
- ✅ Phase 0：契约、状态机、三层验证架构、研究宪法 1.0.0 发布，tag `phase-0-complete`
- ✅ Phase 1 架构决策（ADR-0021 ~ 0024）与数据基础设施：本地存储、Iceberg Catalog、生产表、归档采集、严格解析、修订存储、REST 补尾（D0 ~ D3E 均已独立验收）
- ✅ 全阶段框架代码（Phase 0.5、2 ~ 14、研究控制台）
- ✅ 2026-09-30 第一轮补全：整合线、ADR-0098（P11 权威）/ ADR-0099（P7 时间序列排名 / 分位）、深度审查修复，经 PR #17 合入 `main`（未测试）
- ✅ 2026-10-01 第二轮补全：ADR-0100 批次与审查修复，经 PR #18 合入 `main`（未测试）
- ✅ 2026-10-01 PR #19 合入 P11 权威证据与 P12 提案审计只读视图（改动含测试文件；最新主线未统一运行门禁）
- ✅ 2026-10-02 第三轮代码收口：ADR-0101 ~ 0106、0108 ~ 0110 实现并整合到 `phase/1`，契约升至 2.6.0；全仓门禁通过；旧分支轮候选归档清理

## 4. 当前正在做

- ✅ 第三轮代码收口（2026-10-02，`phase/1@1536a409`）：单元级提交（ADR-0108）及审查发现的两处修复（窄证明须复核单元已提交内容；无已提交元素的归档单元判为不完整）、Dataset v3 生产入口（ADR-0101，含 listing 质量报告入口）、契约 2.6.0 旧质量表绑定（ADR-0109）、State 入口（ADR-0102）、P7 计划绑定与重启恢复（ADR-0103 / 0110）、paper deviation 绑定（ADR-0104）、P11 运维收口（ADR-0105）、P14 迁移目标（ADR-0106）。明细见[剩余代码计划](docs/plans/2026-09-28-remaining-code-gaps.md)。
- ✅ 全仓门禁（四段）：10570 passed / 148 skipped / 0 failed；ruff、format、mypy 通过。
- 🔄 PostgreSQL 启用用例实跑；随后经 PR 合入 `main`，再在 `main` 上重跑 E1-CAP-1（含 K = 0 / 1000 / 3000 预填充历史）。

## 5. 下一步

### 我（Raphael）需要做

- 继续逐模块调试；定向测试通过不替代全仓门禁或 Phase 验收
- 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，以消除授权冲突（见 §6 第一项）
- 知识条目标签 / 资产的具名人工审阅；Profile 数值、交易 / 风险预算与实盘保持冻结 / 关闭

### Claude Code 需要做

- 合入 `main` 后按 ADR-0108 §9(d)(e) 重跑 E1-CAP-1（含预填充历史场景 K = 0 / 1000 / 3000）；32 MiB 门槛不变
- 逐 Phase 验收仍是独立工作；全仓门禁通过不等于 Phase 验收

## 6. 当前待决策

**D-AUTH-CONFLICT（开放，最高优先）**：CLAUDE.md §0 同时包含 2026-09-28 Claude Code PM 授权与 2026-09-30 早段 Codex PM 段落（`fc9f643`），互相冲突。Raphael 于 2026-09-30 在 Claude Code 主会话中指令：「现在你接管这个项目」「给你最大权限 能改所有的代码 文件内容」「你不管文档怎么说 能写的都写了」「你自己决定一切 我不知道也不懂」。本轮按该指令由 Claude Code 执行；CLAUDE.md 自身的修改被环境安全检查拦截（不得自我修改授权文件），留待 Raphael 本地删除 §0 中 Codex 段落以消除冲突。

**开放的阻塞 / 待办**

| ID | 事项 | 状态 |
|---|---|---|
| E1-CAP-1 | Phase 1 完整进程 32 MiB 容量门 | ADR-0108 已实现（2026-10-02）；待合入 `main` 后重跑正式矩阵与预填充历史场景，阻断 Phase 1 验收 |
| D-META-AGE | 单元级提交后，Iceberg metadata 仍随表的历史单元数 K 线性增长 | ADR-0108 残余问题；日归档回填到 2017 年时，每张 Canonical 表约 6,700 单元，解析后 metadata 约 130–200 MiB（估算）；等 ADR-0108 第 9(e) 条的 K 轴数据出来后另立 ADR；不阻断 E1-CAP-1 随 N 的判定，但阻断生产规模回填 |
| W1 | 全仓测试门禁 | ✅ 2026-10-02 `phase/1@1536a409` 全绿（10570 passed / 148 skipped）；PostgreSQL 用例实跑中；之后每次合并须复跑 |
| P11-RUN | P11 真实运行 | 需要部署设置 |
| P0.5-REVIEW | 知识库种子标签 / 资产 | 需具名人工审阅 |
| D-CATALOG-TABLES | 真实 Catalog 建 `event.*` / `state.*` 表 | 已授权，尚未执行 |
| D-09 | Profile 数值（TBD-1 ~ TBD-5） | 未冻结，Phase 4 校准后决定 |
| STAGING-RETAIN | 本地存储 publish 后保留 staging 条目，inode 只增不减 | FOLLOW-UP，清理策略须另立 ADR（见剩余代码计划） |

**已决定（仅列索引，详见 ADR 与 Git 历史）**：D-V3-LEGACY-BIND → ADR-0109；P7 重启恢复 → ADR-0110；D-P11-WINDOW → ADR-0105；P14-TARGET → ADR-0106；D-0108 → ADR-0108；D-P11-AUTH → ADR-0098；D-P7-OPS → ADR-0088 / 0099 / 0100；P12-LOOP → ADR-0100 §7（取代原暂缓决定）；D-LIST → ADR-0051（政策 1.1.0）；D-HIST → ADR-0032；D-QGAP → ADR-0031；D-33 → ADR-0027；D-FLOAT / D-PFIELDS / D-CTRL → ADR-0052；D-VFAIL → ADR-0053；D-PARTIAL → ADR-0054；E1 各子决定 → ADR-0075 / 0077 / 0093 / 0094 / 0097；D-STATE-INC 暂缓（ADR-0035）。

## 7. 当前风险

- ⚠️ 全仓门禁已通过，但测试通过不等于 Phase 验收；E1-CAP-1 与各 Phase 验收矩阵仍未完成
- ⚠️ 授权文件内部冲突（§6 第一项）未消除前，后续代理可能读到相互矛盾的指挥关系
- ⚠️ Iceberg metadata：ADR-0108 已实现按单元提交；仍随历史单元数线性增长（D-META-AGE），生产规模回填前须再决定
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
| 2026-10-02 | 第三轮代码收口 + 全仓门禁 | ADR-0101 ~ 0106、0108 ~ 0110 实现整合于 `phase/1@1536a409`；门禁 10570 passed / 0 failed |
| 2026-10-02 | 契约 2.6.0（ADR-0109） | v3 manifest 的旧质量表"有 snapshot 才绑定"；旧版本规则与哈希不变 |
| 2026-10-02 | ADR-0108 审查修复 | 窄证明复核单元已提交内容（伪造行回归）；无已提交元素的归档单元判为不完整 |
| 2026-10-02 | ADR-0108 Accepted | Canonical 与 Raw 归档元素改为一个逻辑单元一个 snapshot；只改 infrastructure、不改契约 |
| 2026-10-02 | E1-CAP-1 正式矩阵完成（`main@50d6bb6`） | 数值 PASS（证据级）；元数据线性增长使其不能关闭 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件：全部满足**（2026-09-24）——宪法 1.0.0 Approved 且无数值阈值；Profile 与实验元数据契约已定义；核心实体契约与 Schema 导出（收口时 38 份，现为 148 份，契约 2.5.0）；状态机只允许定义的转移；契约层无基础设施依赖；本地测试命令可运行。

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #21 全部满足。当前 #1 ~ #12 与 #17 满足；全仓门禁 2026-10-02 通过；E1-CAP-1 容量门待在 `main` 上重跑。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 全仓门禁已通过（2026-10-01）；Phase 验收完成前，不把模块视为已验收。
2. 在本地删除 `CLAUDE.md` §0 中的 Codex PM 段落，消除授权冲突。
3. 系统没有下单能力，没有策略被验证或晋升，Profile 数值未冻结——这些保持不变。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 合入 `main` 后重跑 E1-CAP-1 正式矩阵与预填充历史场景（ADR-0108 §9(d)(e)），据结果关闭或另立 ADR。
2. 按 roadmap 逐 Phase 准备验收证据；全仓门禁通过不等于验收，每次合并前复跑门禁。
3. FOLLOW-UP：P11 解析重复复核同一 manifest、本地存储 staging 条目保留、D-META-AGE，各自需要时另立任务 / ADR。
4. 真实 Catalog 建表仍是独立运行操作；不得实盘、不猜 Profile 数值、不打开 P7 执行或 P12 循环内提案开关。
