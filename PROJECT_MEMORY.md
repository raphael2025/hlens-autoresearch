# HLENS-AutoResearch Project Memory

> 给 Claude 的长期项目记忆：只保存跨会话仍然有效的事实。
> 维护规则见 `CLAUDE.md` §8（目标 < 200 行，> 300 行必须 Compaction）。
> 当前进度看 `PROJECT_STATUS.md`；完整架构看 `docs/architecture/`；决定全文看 `docs/adr/`。
> 2026-10-01 更新恢复点：PR #17–#19 已合并至 `main@b5f80fe`；E1 bounded 生产路径已在主线但 E1-CAP-1 / DQ-9 未测；P7-CS-EXEC 代码提交为 `phase/1@775245e`，未验证、未合入。当前代码清单见 `docs/plans/2026-09-28-remaining-code-gaps.md`，进度见 `PROJECT_STATUS.md`。

## 1. Project Identity

- 名称：HLENS-AutoResearch
- 定位：长期演化、模块化、可插拔、可验证的加密市场自动化研究基础设施（不是交易机器人或回测框架）
- 核心目标：持续吸收公开知识、已有策略和失败经验，通过组合与实验验证产生、检验新假设
- Phase 1 数据范围：Binance 公共 spot `BTCUSDT` / `ETHUSDT`，归档 aggTrades + 1m klines（ADR-0022）；
  正式研究标的与周期（D-09 提案为 BTCUSDT 1H）仍待 Phase 4
- 当前阶段：Phase 0 已完成（tag `phase-0-complete`）；Phase 1 已开启、未验收，E1-CAP-1（完整进程 32 MiB 容量门）是验收阻断且尚未在当前代码上测量；全仓门禁从未在当前代码上运行
- 决策权：CLAUDE.md §0 同时包含 2026-09-28 Claude Code PM 授权与 2026-09-30 早段 Codex PM 段落（`fc9f643`），互相冲突；Raphael 于 2026-09-30 在 Claude Code 主会话指令 Claude Code 接管、「你自己决定一切」，本轮据此执行；CLAUDE.md 自身修改被环境安全检查拦截，冲突留待 Raphael 本地删除 §0 Codex 段落（见 `PROJECT_STATUS.md` §6 D-AUTH-CONFLICT）。实盘操作始终需 Raphael 亲自批准；H3 / H4 / H6 不受任何授权改变

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本自已发布的 `2.0.0` 向前兼容演进：2.1.0（ADR-0052）→ 2.2.0（ADR-0055）→ 2.3.0（ADR-0077）→ 2.4.0（ADR-0088）→ 当前 **2.5.0**（ADR-0094 PIT v3；ADR-0100 未改契约版本）；破坏性变化须升 major 并走 ADR。持久化对象按记录版本重放；当前版本新建对象的信封与哈希随 minor 变化属预期
- current Schema **148 份**，与 `CONTRACT_MODELS` 一一对应；legacy v1 35 份（`schemas/v1/`）只读；v1 与 v2 哈希不可比较，读取 v1 不赋予任何登记 / 晋升资格
- 研究 Provider Protocol 0 个是 ADR-0017 的决定；Data Plane Adapter Protocol 3 个（Storage / Catalog / Collector），可复用 suite 在 `tests/contract_suites/`
- Freeze Contracts, Evolve Implementations；四个 Plane：Data / Research / Control / Application；Research ⟂ Application
- PostgreSQL = Control Plane（不存大型行情）；Iceberg / Parquet = 真实来源；DuckDB / Polars 只是计算引擎；
  Iceberg Catalog 用独立 PostgreSQL 库、warehouse 为本地 `file://`、Phase 1 ~ 6 无 NATS（ADR-0021）
- 数据架构冻结正文（表、分区、source / parser / policy / universe 标识符、PIT I/O、依赖与设置字段）：`docs/architecture/03-data.md`
- Provider / Plugin 架构；LLM、Backtest Engine 均可替换；LLM 只产出数据，永不作裁判
- Domain 层无基础设施依赖；依赖方向 apps → application → domain ← plugins/infrastructure；research 依赖 apps/worker 而非相反（ADR-0049）
- 新运行能力一律默认关闭：P7 执行（`P7ExecutionSwitch` 默认关，`TypedPlan.runnable` 默认 False）、P12 循环内替换提案（默认关）、实盘（ADR-0084）
- 全文：`docs/architecture/00-overview.md`

## 3. Current Research Direction

Market State → Feature / Event → Knowledge Retrieval → Hypothesis → Combination → Experiment → Validation → Research Memory → Strategy Evolution

- 新颖性主要来自确定性的组合 / 条件化 / 时序算子，并且每次组合都计入尝试次数
- 路线图：`docs/research/roadmap.md`
- 开发顺序（Raphael 2026-09-27 起）：先补齐并整合各模块底层代码，再由 Raphael 本地统一运行门禁、逐模块调试与验收；代码整合 / 合并不等于 Phase 验收，验证状态必须单独记录

## 4. Current Phase

- Current Phase：Phase 1（Market Representation）已开启（2026-09-24），未验收；D0 ~ D3E 已独立验收，D4 已关闭
- Current Blocker：E1-CAP-1 完整进程工作集（含 PyIceberg metadata、Parser / scan 临时对象、normalizer 状态与 API 返回对象）的 32 MiB 门槛未证明；ADR-0100 已写两轮有界化，未测量。旧候选分支的容量数值不能外推为 `main` 的结果
- 其余 Phase 0.5 / 2 ~ 14 与 apps：代码已写（多数为框架 + 补全），未测试、未验收
- Next Milestone：Raphael 安排统一调试 / 门禁 → 在生产路径与 `main` 一致的提交上测量 E1-CAP-1 → Phase 1 验收。P7-CS-EXEC 已有本地恢复提交。当前代码计划见 `docs/plans/2026-09-28-remaining-code-gaps.md`
- 仍开放：P7-CS-EXEC 尚未进入 `main` / 未验证；E1-CAP-1 与 DQ-9 未测；P11 真实运行需部署设置；知识库种子具名人工审阅；P14 无迁移目标；真实 Catalog `event.*` / `state.*` 建表（已授权未执行）；Profile 数值未冻结

## 5. Active Decisions

**基础与 Phase 0**
- ADR-0001：重大架构决定用 ADR 记录
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- ADR-0003：Python 3.13 + uv，与系统 Python 隔离
- ADR-0004：本地 Git，不改全局配置，一次性提交身份；ADR-0025：远程 = 私有 GitHub `raphael2025/hlens-autoresearch`，CI 未配置
- ADR-0005：研究 / 生产边界 = Artifact + Registry + Promotion + Equivalence Gate；Promotion 链今天拒绝所有策略；Q-1 / Q-2 / Q-3 / Q-7 开放
- ADR-0006：生命周期 v2（OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；ACTIVE 带 `execution_mode`，不设 LIVE 状态）；Q-4 ~ Q-6 开放
- ADR-0007：三层验证（Constitution / Validation Profile / Experiment Metadata）+ 两步冻结
- D-09 数值（TBD-1 ~ TBD-5）与 H-3 ~ H-7：未批准，Phase 4 校准后冻结为 Profile 参数；Constitution 不得出现数值阈值
- ADR-0008 ~ 0016：只读载荷与内容哈希、完整实验身份、构造与版本语法、生命周期主体 / 授权、信息流白名单（Outcome 不得作输入）、确定性判定与拒绝 NaN / ±Inf、Profile 结构不变量、审计身份类型、`LlmCall` 登记结构
- ADR-0017：Provider 方案 B——Protocol / DTO / contract tests 随首次消费的 Phase 交付
- D-25：2.0.0 已发布，同类改变必须升 major
- ADR-0018：三类语义身份排除信封版本；ADR-0019：生命周期转移证据至少一项非空
- ADR-0020：Constitution `1.0.0 / Approved`，正文 sha256 `4d603d62…259cd` 不变，只前向适用

**Phase 1 数据**
- ADR-0021：PyIceberg SQL Catalog on 独立 PostgreSQL 库、`file://` warehouse、Phase 1 ~ 6 无 NATS
- ADR-0022：Binance 公共 spot BTCUSDT / ETHUSDT，归档优先 + market-data-only REST 补尾；无交易能力或密钥
- ADR-0023：历史轴（`available_time`）与知识轴（`knowledge_time`）分离；append-only revision；competing heads fail closed
- ADR-0024：双轴 listing episode 历史；`UniverseSelectionSpec` 在 manifest 绑定
- ADR-0026：PyIceberg day transform 写入用官方 `pyiceberg-core` extra
- ADR-0027（D-33）：REST 四张 additive 表；规范内容逐字段相等才记"归档优先"证据边，否则 fail closed
- ADR-0028：Canonical revision 与 Raw 元素一一对应，跨通道边由 PIT 映射不物化
- ADR-0029：listing 来源 = `exchangeInfo` 快照，`tradable_from` 为本机观察下界
- ADR-0030：FeatureProvider 契约，执行器截断输入 + 因果扰动测试
- ADR-0031（D-QGAP）：质量报告证据缺口写独立只追加表
- ADR-0032（D-HIST，Raphael 批准）：数据集显式绑定时归档可用时间取 `min(存储值, 可观察时刻 + 5 秒)`，未绑定即保守
- ADR-0033：物化 Research Dataset 选择表登记为生产表
- ADR-0051（D-LIST）：上市历史"观察状态回填"假设；政策 **1.1.0** 写入 BTCUSDT / ETHUSDT 下界 2017-08-17（2026-09-30 经 data.binance.vision / data-api.binance.vision 核实）；只是研究假设，不证明真实上市史
- ADR-0075：PyIceberg 固定快照有界扫描（infrastructure 内），delete files fail closed
- ADR-0076：有界 Canonical normalization result
- ADR-0077：有界 Research Dataset 证据（v3 manifest、内容寻址 evidence streams；DQ-1 由 Raphael 批准）；Universe v3 显式 `UniverseRunParams`；DQ-9 数值待容量证据
- ADR-0093：Quality 报告固定大小 manifest + 内容寻址 evidence streams；v1 / v2 只读兼容
- ADR-0094：PIT v3 完整冲突 heads 写有界 evidence stream（契约 2.5.0）；v2 与 2.3 / 2.4 replay 不变
- ADR-0097：PIT bounded graph 用调用级 SQLite scratch index
- D-NET：可下载 BTCUSDT / ETHUSDT 各 1 ~ 3 天官方公共归档，只写本机、不入仓库
- D-E1-CANONICAL-SCRATCH：Canonical 位置索引用 `Settings.canonical_scratch_uri`（默认 `data/scratch`），不回退 `TMPDIR`

**Phase 0.5 / 2 ~ 14 与 apps**
- 框架批次 ADR-0034 ~ 0050：知识库、状态、事件、Outcome + 最小验证门、策略 / 风控 / 回测、状态×策略、假设 + LLM、稳健性、合成市场、路由、事件总线 + worker、进化、模拟执行（无实盘）、迁移、API / Web、持续循环、循环审计契约
- ADR-0052（Raphael 批准）：精确小数、Profile 新字段、负对照独立阈值（2.1.0）；ADR-0053：VALIDATION → FAILED；ADR-0054：部分成交跨 bar 结转
- ADR-0055：知识标签 / 资产检索（2.2.0）；ADR-0058：知识库经人工审阅写入；种子无具名审阅的标签 / 资产
- ADR-0056 / 0057 / 0061 / 0066：事件表、事件 subject、交互 DSL、显式 Event 建表入口（默认无副作用）
- ADR-0059 / 0060 / 0064 / 0065：G4 跨资产 × 横截面、C-T4 市场基准、G4 成交量来源一致、结转余量失败关闭
- ADR-0062：Profile 冻结登记（追加式、目录外锚点）是 Promotion 的权威冻结来源；登记为空
- ADR-0063：本机只读 API 运行时 = Uvicorn（仅 127.0.0.1）
- ADR-0067：P11 显式劣化检查绑定调用方声明证据；ADR-0098：P11 权威 = append-only `LifecycleRegistry` 重放 + 钉定 v3 Dataset source + 与 validation 同源的 metric 闭集（**取代 ADR-0080**）
- ADR-0068 / 0070 / 0071 / 0073 / 0078 / 0083：P7 闭世界 typed plan、失败轮 fail stop、只读复核摘要、PREPARE / TrialLedger / COMMIT admission 恢复、lowered outputs 完整性、失败轮人工重试
- ADR-0074：synthetic-only 本机有限批次 operator；无冻结 Profile 时没有合规运行配置
- ADR-0082 / 0088 / 0099：P7 六类算子语义与 lowering；契约 2.4.0 组合扩展；`rank` / `quantile` 时间序列语义
- ADR-0069：P12 `combine` 对风险 / 适用范围冲突失败关闭
- ADR-0072：Phase 4 交付最小验证门与封存 OOS，Phase 8 交付稳健性扩展
- ADR-0079 / 0081 / 0085 / 0086 / 0087 / 0089 / 0092：paper deviation 声明范围、报告 payload DTO 版本、研究库扩展、验证与生命周期收口、插件发现、State 物理表、Promotion 可信验证重放
- ADR-0084：实盘接口预留，默认关闭
- ADR-0091：登记处完整性审计严格只读；Failure Registry 不提供历史防篡改证明
- ADR-0095：生产 Worker 由部署方显式受信 Runtime Factory 组合
- ADR-0096：TrialLedger 完全相同的登记作只读幂等确认，不增加 trial
- ADR-0100（2026-09-30，Raphael 直接指令）：P7 执行 Provider + allowlist 编译器（默认关）、横截面 `rank_cs` / `quantile_cs` Provider、P11 指标闭集与默认环境、ADR-0051 政策 1.1.0、E1 有界化、P12 可选循环内替换提案；修订 1 细化 1.3.0 时间事件。`main@b5f80fe` 的横截面 Provider 尚未接 compiler；本地 `phase/1@775245e` 已增加专用根 Provider 构造，输出不能直接进入单序列组合器 / Research Loop。
- D-P11-WINDOW（ADR-0049 遗留）开放；D-STATE-INC 暂缓（ADR-0035）

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码；LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则（H3）；不削弱测试（H4）；不删除失败实验或生命周期历史（H6）
- Constitution 原则修改须按第九章另起 ADR 并由 Raphael 批准具体变化
- Outcome 不得作为 Feature / State / Event / Strategy 的输入；任何计算只用 `available_time ≤ t` 的数据
- 所有实验可复现；所有 Schema 版本化
- 不猜测或冻结 Profile 数值；新运行能力默认关闭，验收前不得打开
- 实盘（下单、连接实盘账户、交易凭据）始终需要 Raphael 亲自批准
- 不修改旧项目与外部数据；不 force push、不改写已发布历史、不修改全局 Git 配置

## 7. Current Known Risks

- 两轮大规模补全代码均未测试；首次全仓门禁可能暴露大量失败
- pypi.org 索引域名在本机被阻断（files.pythonhosted.org 可达）：离线安装用 uv.lock 精确版本
- WSL 内存约 15 GiB；Docker 未安装；外部数据盘未挂载；warehouse 无异地副本；CI 未配置
- 官方资料不能证明任何历史 revision 的公开时刻：`binance.spot.publication@1.0.0` 一律 `available_time = ingest_time` + 证据缺口，未绑定 ADR-0032 假设时早于本机 ingest 的历史不可用；归档替换一律 competing heads
- REST 同样无公开时刻证据；未证明 aggTrade ID 连续、REST 与归档内容一致、未结束 K 线可判别——一律 fail closed
- D-33 精确比较：REST 毫秒 vs 2025 年起归档微秒，带亚毫秒位的成交无法证明相等
- 契约层只校验结构与声明：依赖闭包、trial 权威账本、Registry 存在性、泄漏检测、哈希与真实内容一致、Profile 已冻结等属运行时服务义务，不得宣称已防住
- `LlmCall` 与生命周期证据只保证登记结构非空；内容可取回、批准人权限与职责分离未实现
- 契约层不拒绝 `to_mode = LIVE`；运行时拒绝 LIVE，无下单接口或凭据
- JSON Schema 在几处弱于运行时模型；权威校验必须经过运行时模型
- `venue` / `symbol` / `timeframe` 区分大小写、不做规范化；未来 Adapter 必须产出规范值
- 外部是否存在 v1 历史数据证据不足：不得宣称迁移路径已在真实数据上验证
- 后代 G5：每个 family 只评估一次密封 OOS（C-S1..3），循环内替换提案必须使用独立预登记密封窗口，绝不复用已开封窗口
- 旧研究可能已看过 BTC 全部历史：历史封存区在认知上不完全干净

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（尚未评估内容）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任
- Phase 0 审查链：C1 `FIX_BEFORE_CLOSE` → ADR-0018 / 0019 → C3 `READY_FOR_HUMAN_CONSTITUTION_GATE` → ADR-0020；记录见 `docs/reviews/`
- 授权演变：2026-09-23 Codex 协调者 → 2026-09-24 "授权所有"（Codex）→ 2026-09-28 Claude Code PM → 2026-09-30 早段 Codex PM 段落 → 2026-09-30 Raphael 在主会话指令 Claude Code 接管（冲突见 §1）
- 2026-10-01：17 个 Codex 临时分支 tip 已保存至 `refs/archive/2026-10-01/branches/codex/`，分支与干净 Codex worktree 已清理；Claude feature worktree 的未提交 / 未合入内容仍需逐项审阅，不计为主线完成。具体当前数量见 `PROJECT_STATUS.md` 快照。

## 9. Last Known Good State

- Phase 0 基线：tag `phase-0-complete`（唯一经完整门禁的发布基线）
- `main` = `b5f80fe`（PR #17–#19 已合并；PR #19 增加 P11/P12 只读审计视图）——最新 HEAD 未跑统一门禁；Phase 1 E1-CAP-1 未测量
- 当前工作分支：`phase/1@775245e`，从计划提交 `830e267` 后新增 P7-CS-EXEC；代码 / 回归用例未运行，未合入 `main`，不计为主线完成。
- `origin/main` 与本地 `main` 已同步；PR #19 分支已合并，远端当前没有开放 PR。Claude feature worktree 有未提交或未合入内容，均未计入完成状态。
- 下一步：由 Raphael 安排后续统一测试 / 调试与容量测量；当前 P7 实现仍未验证或合入 `main`，不得视作主线完成或 Phase 验收
