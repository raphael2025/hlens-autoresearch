# Claude 自主决策记录（2026-09-26）

授权来源：Raphael 于 2026-09-26 在会话 "2026-09-26 全阶段代码完成计划" 中设定目标——"完成这个项目的所有代码工作，**所有的决策都由你来决定，包括红线的事情**，并生成相关的文档"。
决策者：Claude Code（Opus）。本文件逐项记录此前挂起的决定、裁决、理由与实施位置，供 Raphael / Codex 事后复核或推翻。
"决定不做"也是决定：凡属不可逆、对外或缺乏证据的事项，本记录选择保守方案并写明理由。

| ID | 问题 | 裁决 | 理由 | 实施 |
|---|---|---|---|---|
| D-FLOAT / D-PFIELDS / D-CTRL（ADR-0052） | 验证契约精确小数、Profile 新字段、负对照独立阈值 | ~~保持 2.0.0~~ **被 Codex 全代码复核 K3 取代**：按 Accepted ADR 以 2.1.0 实施，旧 2.0.0 数据须原样可读可重放；不能证明时保留旧行为并交付阻断证据 | 结果：**阻断**——`infrastructure/canonical/rules.py:670` 把在用的 `CONTRACT_SCHEMA_VERSION` 写入每行 Canonical，重放经 `row_integrity.py:347-360` 重算批次指纹、`normalizer.py:1212` 逐列比较；只把版本改为 2.1.0 即令已提交单元的重新处理失败（`CatalogIntegrityError`），修复须改 Phase 1 基础设施（Codex 复核中） | 证据测试 `tests/infrastructure/canonical/test_contract_version_replay.py`（`xfail(strict=True)`）、2.0.0 金标准向量 `tests/golden/v2_0_0/`、ADR-0052 "Implementation blocker (2026-09-26)"；部分实现停放在本地分支 `wip/adr-0052-exact-fields`（`8e4a71c`，不得合并） |
| D-VFAIL（ADR-0053） | VALIDATION → FAILED | 集成集成会话已完成的实现（`0d5a975`） | 已由 Raphael 批准的 ADR，实现现成，避免重复 | core 通道 |
| D-PARTIAL（ADR-0054） | 部分成交跨 bar 结转 | 集成现成实现（`d640eff`，不升契约版本） | 同上 | core 通道 |
| P3-EVTABLE（ADR-0056） | 物理 Iceberg 事件表 | **Accepted** 并实现（新模块在 `infrastructure/event/`，不改 Phase 1 表 / 定义 / 供给脚本，不在真实 catalog 建表；只读核实任何持久 catalog 均未建过该表） | roadmap Phase 3 需要持久事件表；同 ADR-0031 / 0033 的只追加表先例 | P3 通道 |
| P3-MULTISYM（ADR-0057） | `EventRequest` 多标的 | 方案 A：可选 `subject`（一请求一标的；调用方提供的稳定、大小写敏感 opaque ID） | 语义经 Codex `648fe6c` 认可；**但**新字段在 2.0.0 下写成，按 Codex K3 / K5 不接受为契约完成 | 代码在 `564c87c`（保留恢复点）；待 ADR-0052 版本化重放后以 2.1.0 重新声明 |
| P05-WRITE（ADR-0058） | 知识库写入路径 | **Accepted**：只有 Python API / CLI、每次写入要求审阅人、只追加；无 HTTP 写端点、无新 Protocol | 满足 knowledge-base.md "人工审阅后入库"，且不越过 ADR-0048 只读边界 | `plugins/knowledge/store.py`、`cli.py` |
| P10-ELIG | 路由资格绑定验证证据 | 研究层可选"证据模式"：核对报告哈希、主体、PASS 与 G5；信任模式哈希不变 | 不改契约即可关闭研究侧缺口；生产资格仍属 Control Plane | P10 通道 |
| P8 单标的 | 回测验证只支持单标的 | 研究层按标的拆分 Outcome 请求并保守合并；单标的路径逐字节不变 | 不改契约 | P8 通道 |
| P6 条件假设入循环 | 是否把矩阵条件假设接入持续循环 | 可选开关（默认关闭）：开启时先登记全部单元再看结果，计入族 trial 数 | 预先承诺 + 诚实的 trial 计数；默认关闭保持既有记录哈希 | P6 通道 |
| D-MINEFF | 条件假设的 `minimum_effect` 是假设内容还是门槛 | **假设内容**（研究者预先声明），不作验证门槛；生产路径无默认值 | 与宪法"阈值只来自 Profile"一致 | 无代码变化（现状即如此） |
| D-DEP（ADR-0049） | research 依赖 apps/worker | **维持** | apps 不依赖 research，边界测试不变；迁移成本高、无收益 | 无 |
| D-LIST（ADR-0051） | 上市历史假设 + 一次 exchangeInfo | ~~接受方案 A 的原则~~ **撤回：保持 Raphael 的明确暂缓**（`ARCHITECTURE_DECISION_REQUIRED`） | Raphael 2026-09-26 对该项明确"后面再授权 我不知道这个是什么"；具体暂缓优先于其后的一般授权（CLAUDE.md §0：文档冲突即停止；含糊答复不视为批准）；且该项涉及对外网络调用 | 草稿实现只留在本地未推送分支（标注 NOT APPROVED），不合并 |
| P5-PLUGIN | 把 TSMOM 放进 `plugins/` 作生产 StrategyProvider | **不做** | 晋升须有 G0–G5 + 纸面证据；目前没有任何策略通过验证，无证据晋升违背宪法证据原则 | 无 |
| D-09 TBD-1..5 | Validation Profile 数值 | **不冻结** | 只有合成市场的冒烟规模校准证据，没有真实数据校准；此时定数值等于猜测，会让之后的验证结论失去意义 | 无；Phase 9 工具已能产出冻结所需证据 |
| H10 实盘 | 交易端点、账户凭据、真实下单 | **不做** | 不可逆的资金风险；没有风险预算与账户授权；Phase 13 保持模拟 | 无 |
| 合并 `main` / tag | 是否合入 `main`、打 tag | **不做** | Phase 1 仍在 Codex 复核，其他 Phase 未验收；只推 WIP 分支 | 无 |
| 宪法原则 | 是否修改 | **不改** | 无需要修改的发现 | 无 |

复核建议：以上"不做 / 推迟"项都可以由 Raphael 随时推翻；推翻时只需在本表对应行记录新裁决并按 ADR 流程实施。

**开放项（交 Codex）**：ADR-0054（部分成交结转）与 ADR-0057（`EventRequest.subject`）都是在 2.0.0 下加的可选字段（缺省不进哈希）。若 Codex K3 的"不得把新增字段伪装成 2.0.0"
也适用于它们，则须在 ADR-0052 的 Phase 1 重放路径落地后一并迁到 2.1.0；本记录不自行改变。

**追加裁决（2026-09-26 晚，依 Raphael "通过 ADR 自主决定、含红线" 的授权）**

| ID | 问题 | 裁决 | 理由 | 实施 |
|---|---|---|---|---|
| ADR-0059 | G4 跨资产检查对横截面策略结构性 FAIL | **Accepted**：C（零敞口单标的重跑 → INCONCLUSIVE）+ A（显式声明的横截面策略按子宇宙检验） | C 只把结构性不适用的 FAIL 变为 INCONCLUSIVE，仍阻止晋升，不多放行；A 是更强的 C-R3 检验 | B29 |
| ADR-0005 实施 | Promotion 链无代码 | 按已 Accepted 的 ADR-0005 直接实施 Registry / Promotion 服务 / Equivalence Gate，失败关闭 | 无新决策；开放选择写入 ADR-0005 实施说明 | B28 |
| ADR-0060 | C-T4 `market_benchmark_rule` / `inverse_control_reported` 无语义、无代码读取 | **Accepted**：已登记的规则名（`none` / `buy_and_hold_equal_weight` / `flat`，未知 → INCONCLUSIVE）；市场基准与反向对照为报告项，不作否决 | C-T4 的门槛是空模型（已实施）；给每个类别发明超额阈值会违反"阈值只来自 Profile" | 待实施（下一空闲通道） |
| ADR-0061 | Phase 3 交互 DSL 缺失 | **Accepted**：JSON 表达式树（`ref` + `seq` / `and` / `not` / `count`），编译为普通交互规格，逐跳复用上游核对；`not` 的事件时间取窗口结束 | 数据而非代码，只到已审阅算子；无契约变化 | 待实施（下一空闲通道） |
| API → Worker 端到端 | 计划要求"至少一条端到端流程从 API 进入 Worker 再由 Web 读回"，但 ADR-0048 规定 API 只读 | **维持只读**：端到端流程定义为 Worker 作业 → 结果日志 / 报告文件 → 只读 API（`/jobs`、`/reports`）→ Web；不增加写端点 | 写端点会让 Web 能触发研究运行，扩大攻击面且无审批通道；只读链路已有测试（`test_api_jobs.py`、`test_reports_hook.py`、node 测试） | 无代码变化；记录于此 |
| D-L6-1（ADR-0061） | `not` 无法用单一交互规格表达而不引入未来函数 | 编译为 `event_window_end` → `event_absence` 两个规格；ADR-0061 §2 修订 | 契约只有一个 `observable_lag`；两规格形式在窗口结束触发且窗口内所有 B 可见 | B36 |
| D-L5-1（ADR-0060） | 市场基准是否默认启用 | 先作为显式 opt-in 集成；随后在循环 / 合成校准 / 夹具中强制启用并把 TEST ONLY 夹具改为已登记规则名（L7 通道，固定哈希按此有意重新固定） | 未登记规则名须 INCONCLUSIVE；夹具使用占位名 | B35 / L7 |
| 状态目录单写者锁 | L2 跨进程测试发现 `state_dir` 无自身锁 | 实施 `state.lock`（实现缺陷修复，非架构决定） | 注入总线时第二个进程可并发写 | B32 |

**结果更新（2026-09-26 深夜）**：ADR-0052 已按 Codex K3 以 2.1.0 实施（独立 Phase 1 分支 M0～M3 + 门禁修复，B38 / B40），并与 ADR-0054 / 0057 的 2.1.0 重新声明一起合入全代码分支（B41）；
上表 ADR-0052 "阻断"与 P3-MULTISYM "未接受"两行的状态因此变为"已实施、待 Codex 复核"。Phase 1 候选与 `main` 均未合并。

