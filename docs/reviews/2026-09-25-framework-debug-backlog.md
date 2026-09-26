# 全阶段框架批次：调试待办与已知缺口（2026-09-25）

来源：Raphael 2026-09-25 指示"先按框架实现所有代码，每一步更新文档，开发完成后再逐个调试"。
本文件汇总各框架批次子代理自报的缺口、cursor-agent 只读复核的发现与已做的修复，作为逐个调试的工作清单。
所有条目的代码状态均为 **FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**；Profile 数值一律 TBD；没有实盘能力。

## A. 复核发现与处理

| # | 严重度 | 位置 | 发现 | 处理 |
|---|---|---|---|---|
| R1 | 高 | `research/validation/controls.py`、`pipeline.py` | 研究对象可把标签符号预烘焙进交易方向，负对照被打散后仍"通过" | ✅ 已修（ADR-0041）：流水线只用盲化标签计算方向，新增 `G1.label_blind_sides` |
| R2 | 高 | `research/validation/stats.py` | 嵌套重叠区间使有效样本数被高估 | ✅ 已修：改用 `max` |
| R3 | 高 | `research/validation/sealed_oos.py` | 换族名可反复开封；开封后可无限次读取 | ✅ 已修：全局开封预算（必填参数），每次开封只允许一次评估 |
| R4 | 高 | `research/validation/pipeline.py` | G2 / G3 统计未应用 purge / embargo / walk-forward | ✅ 已修：只在 Profile 驱动的清洗后测试折上统计，新增 `G2.walk_forward_folds` |
| R5 | 中 | `research/validation/splits.py` | `purged_k_fold` 不排除封存窗口 | ✅ 已修 |
| R6 | 中 | `apps/execution/service.py` | 持有 `service.venue` 可绕过已跳闸的 Kill Switch（仍只是模拟） | ✅ 已修（809182e）：场所自身绑定并检查 Kill Switch |
| R7 | 中 | `apps/execution/strategy_source.py` | P5 → P13 接线把权重直接当作数量 | ✅ 已修（ffdd5d5）：必填 `PositionSizer`，v1 按权益 / 价格定量 |
| R8 | 中 | `apps/worker/loop.py` | 阶段实际用量超出预估时，事后才发现（超支一次后停机） | ✅ 已修（W2，deb590a；R8 另见 R24） |
| R9 | 中 | `research/loop/stages.py` | 循环审计记录的哈希含浮点数（跨平台可能分叉） | ✅ 已修（W2，deb590a：固定精度 Decimal 字符串） |
| R10 | 低 | `research/loop/memory.py` | LLM 草稿审阅无身份门禁 | ✅ 已修（W2，deb590a：必须给出非循环自身的审阅人） |
| R11 | 低~中 | `infrastructure/strategy/signals.py`、`infrastructure/event/inputs.py` | `available_time = evaluation_time` 依赖上游契约保证因果 | ✅ 已由契约保证：`FeatureValue` / `StateValue` 构造时即拒绝 `latest_input_* > evaluation_time`（`core/contracts/feature.py`、`state.py`），执行器再以 `check_answers` 逐个复核可见集合；适配器只做映射 |
| R12 | 低~中 | `infrastructure/event/runner.py` | 稀疏检查点网格下，检查点之间的回填对执行器不可见 | 已在 ADR-0036 记录；调试时评估默认网格 |
| R13 | 高 | `research/validation/robustness.py`（容量） | `min_capacity` 缺失只记入缺失字段、不产生门，整体可 PASS；`G4.capacity.estimated` 恒为 PASS | ✅ 已修（ADR-0041 实现说明）：估计值只报告；缺 `min_capacity` / 冲击系数 = INCONCLUSIVE 门；`RobustnessCheck` 拒绝无门的缺失字段 |
| R14 | 高 | `research/validation/robustness.py`（参数邻域、时间对齐、延迟压力、跨资产） | 无邻点、偏移为空、`delay_stress_bars = 0`、未声明标的时 `gates=()`，检查被静默跳过 | ✅ 已修：均为 C-R1 ~ C-R5 必需检查，改为 `configuration_missing:<what>` INCONCLUSIVE 门；逐项依据见 ADR-0041 实现说明 |
| R15 | 中 | `research/validation/robustness.py`（C-R2） | 只要样本充足状态净收益 > 0 即通过，欠采样状态的 P&L 集中度被忽略 | ✅ 已修：始终报告欠采样 P&L 占比；有显式参数 `param:state.max_undersampled_pnl_share` 时设门，无参数且占比为正时 INCONCLUSIVE（无发明阈值） |
| R16 | 中 | `research/validation/splits.py`、`robustness.py`（walk-forward） | `step < test_window` 时窗口重叠，正收益窗口比例被抬高 | ✅ 已修：G4 只计不重叠的测试窗口（`non_overlapping_windows`），跳过数写入报告 |
| R17 | 中 | `research/validation/overfitting.py`（CSCV） | CSCV 分块之间不做 purge / embargo | ✅ 已修：按 Profile `data_split.embargo` 剔除样本外分块两侧的样本内时期；剔除过多则 INCONCLUSIVE |
| R18 | 记录 | `research/strategies/validation.py`、`research/validation/report.py` | 预填 `FixedSides` 的来源限制；`validate` 不含 G5 | ✅ 已记录：文档写明 G0 – G4 PASS 无 G5 永不可晋升；报告视图 `promotion` 块与 `BacktestValidation.promotion_blocked_reason` 结构标注 |
| R19 | 高（语义） | `research/loop/stages.py`（`MemoryStage`） | 样本内 G0 – G4 PASS 即移到 OOS，即使 G5 未运行 | ✅ 已核对（行为不变）：ADR-0006 §1 / 07-validation §3 定义 OOS = "正在经过封存样本外检验"，边 `VALIDATION → OOS` = in-sample gates passed、`OOS → PAPER` = sealed OOS passed；文档与转移原因写明"有资格进入 G5"，证据为样本内报告；测试覆盖无 G5 PASS 不越过 OOS（ADR-0049 实施说明「review fixes 2」） |
| R20 | 中 | `research/loop/trials.py`（`OosUnsealBudget`） | 开封预算在配置时一次签名，无人值守循环可耗尽全局开封额度 | ✅ 已修：`approved_families` 逐族列出批准人（写入该族 `OosUnsealing.approved_by`），循环只开封名单上的族；空名单 / 自动化身份被拒 |
| R21 | 中（真实缺陷） | `research/loop/trials.py`、`segment.py`、`research/validation/sealed_oos.py`、`pipeline.py` | G5 先开封并释放封存 bar，之后提前返回（无封存决策时刻 / 无非零仓位）时开封已消耗却无评估记录，vault 仍允许他人读一次 | ✅ 已修：`claim_evaluation` 在释放前原子消耗唯一评估；`SealedBars.release` 只接受一次性凭据；提前结束或出错 → INCONCLUSIVE `consumed_without_result`，窗口永久关闭；两条路径均有回归测试 |
| R22 | 中 | `research/validation/overfitting.py`（CSCV） | purge 只用时期时间 ± embargo，标签 / 持有期可能长于 embargo | ✅ 已修：必填 `horizon`（`RobustnessInput.holding_horizon` = max(标签 horizon, 最长持有期)），与 `purge_and_embargo` 同一语义；剔除过多仍 INCONCLUSIVE（ADR-0041 实施说明「review fixes 2」） |
| R23 | 低 | `research/validation/robustness.py`（walk-forward） | 无收益的窗口被移出分母，抬高正收益窗口比例 | ✅ 已修（保守选项）：空窗口计入并报告，任一空窗口 → `G4.walk_forward.positive_fraction` INCONCLUSIVE（不计为非正：那会把缺证据变成 REJECTED）。连带：循环多轮 E2E 的第 0 轮从 PASS 变为 INCONCLUSIVE（每轮只验证新段，Profile walk-forward 覆盖整个研究窗口），见 ADR-0049 实施说明第 5 条 |
| R24 | 低 | `apps/worker/loop.py`（预算） | 只有阶段多报时停机，少报时预算按少报额计费 | ✅ 已修（保守选项）：完成的阶段按逐维度 max(声明, 报告) 计费，`StageRecord.charged` 记录 |
| R26 | 中 | `research/validation/sealed_oos.py`、`research/hypotheses/ledger.py`、`research/evolution/lineage.py` | 开封账本（含 `claim_evaluation` / `mark_evaluated` 与逐族批准）、`TrialLedger`、`LineageGraph` 只存在于内存：进程重启后"每族只开封一次""全局开封预算""族 trial 计数""谱系父子关系"都不再跨进程生效 | ✅ 已修：新增共享模块 `research.persistence.AppendOnlyJournal`（哈希链、只追加 JSON-lines，写法对齐 `FailureRegistry`）；三者的构造函数新增可选 `path`（省略即原有纯内存行为，向后兼容），给定时落盘并在重新打开时重放校验整条哈希链；篡改、截断或未知记录类型一律拒绝，不静默修复（ADR-0040 / ADR-0041 / ADR-0045 同日实施说明） |
| R25 | 中（设计） | `research/loop/stages.py`、`trials.py`、`segment.py`、`research/hypotheses/ledger.py` | 循环每轮只验证新段（3 天），而 Profile 的 walk-forward 覆盖整个研究窗口，G4 walk-forward 结构性 INCONCLUSIVE，真实效应永远到不了 OOS（R23 连带） | ✅ 已改（决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线）：实验 / 验证在截至 `as_of` 的累计研究数据上进行，封存窗口仍永不进入；每个（假设，轮次）评估都是 TrialLedger 中单独预登记的 trial（`register_reevaluation`），族 trial 数随之增长、G3 校正随之加强；只重新评估 VALIDATION 中 INCONCLUSIVE 且数据已增长的假设，REJECTED / FAILED 永不、OOS 不再样本内重跑。E2E：植入效应第 0 轮 INCONCLUSIVE，第 1 轮累计数据覆盖 walk-forward 后 PASS → OOS；纯噪声不通过（ADR-0049 accumulated validation window 实施说明） |

## B. 需要 Raphael 决定（红线，Claude 不自行决定）

| ID | 问题 | 推荐 | 不决定时 |
|---|---|---|---|
| D-FLOAT | `GateResult`、Validation Profile 阈值等核心域模型在哈希载荷中使用浮点数（改为 Decimal 属破坏性契约变更，H1） | 另起 ADR：新 major 或新增 Decimal 字段并弃用浮点字段 | 保持现状：同平台可复现，跨平台不保证（ADR 已如实记录） |
| D-PFIELDS | Validation Profile 缺少容量、跨资产一致性、CSCV 分块数、开封预算、冲击系数等字段（改 Profile 结构属 H2） | 另起 ADR 增加这些字段（数值仍待校准后冻结） | 这些规则只能由调用方显式传参；未传时判为 INCONCLUSIVE，绝不判通过 |
| D-CTRL | 校准证据（P9 × P8）：`significance.multiple_testing_threshold` 被两处反向使用——G3 要求校正后 p ≤ 阈值，G1 负对照要求对照 p ≥ 阈值；因此"放宽"阈值反而让负对照全部失败，Profile 无法单独调节两者 | 与 D-PFIELDS 合并起 ADR：为负对照增加独立字段 | 保持：两者共用一个阈值（偏保守，不会多放行） |
| D-VFAIL | 生命周期只允许 CANDIDATE → FAILED；验证阶段的技术故障（C-P3）无法把对象标为 FAILED（ADR-0006 状态机属冻结契约） | 另起 ADR 增加 VALIDATION → FAILED（需证据） | 失败记录照写进 Failure Registry，生命周期停在 VALIDATION |
| D-MINEFF | P6 条件假设登记要求 `minimum_effect`（测试中用 `"0.1"`）：它是假设字段还是验证阈值？ | 视为预登记的假设字段（研究者声明），不作为验证门槛 | 测试用值只存在于测试中；生产路径不设默认 |
| D-PARTIAL | 回测部分成交的剩余量能否跨 bar 结转？冻结契约不允许：`BacktestResult.check_answers` 要求每笔成交都在该目标的执行 bar、每个目标至多一笔成交（`成交数 + 未执行目标数 <= 目标数`），结果无剩余量字段，`PriceBar` 无成交量字段，descriptor 的 `execution_model` 只有 `next_bar_open` | 另起 ADR（additive，Codex 可在授权内批准）：新增执行模型字面量（如 `next_bar_open_participation`）、按执行模型放宽成交 bar 规则与一目标一成交的计数、结果增加剩余量字段、`PriceBar` 增加可选 `volume`（须证明省略时既有哈希不变） | 保持现状：`ExecutionModel` 在执行 bar 截断并取消剩余量、在 `ExecutionReport` 中报告；策略每个决策时刻重发目标即按实际持仓逐 bar 收敛 |

## C. 各批次自报的已知缺口（调试时逐个处理）

- **P3 事件**：一次请求只覆盖一个标的；检查点网格代价约为检查点数 × 可见输入数；~~交互规格声明的上游只做形式检查~~ ✅ 已修（2026-09-26）：`run_events` 经 `infrastructure/event/upstream.py` 核对交互的上游规格（恰为声明的 `lineage` 事件引用、spec hash 被 trigger 绑定、上游事件属于它们且哈希一致、Feature / State 声明**等于**上游并集，可选核对上游结果），给出特征 / 状态运行时逐点重算 `source_lineage_hash`；任一不符 fail closed（ADR-0036 Implementation note, interaction upstream verification；回归测试 `tests/infrastructure/event/test_upstream_verification.py`）；~~spec hash 按 trigger 子串匹配（重叠十六进制可误判）~~ ✅ 已修（2026-09-26）：解析规范 JSON trigger，`<name>` / `<name>_hash` 字段逐字相等才算绑定；核对函数返回已做 / 未做 / 不适用的核对，`run_events(..., require_full=True)` 在缺证据时拒绝，P3 冒烟改用它（ADR-0036 Implementation note, durable review fixes）；尚无物理 Event 表。
- **P4 Outcome / 最小验证门**：负对照为单次固定种子；开封记录与 Outcome 表只在内存中；统计用浮点正态近似。
- **P5 策略 / 回测**：~~执行模型单一（下一根开盘成交、无部分成交 / 融资 / 冲击）~~ ✅ 已补（2026-09-26，可选）：`BarBacktester(execution=ExecutionModel(...))` 提供 bar 成交量参与上限、与 G4 容量检查同一公式的平方根冲击、逐 bar 步借券 / 现金融资（计入权益），全部参数显式、无默认，经 version `1.1.0+exec.<fingerprint>` 绑定进 `provider_hash`；默认构造与 v1 逐字节相同（ADR-0038 实施说明 execution realism；`tests/plugins/backtest/test_execution_model.py`）。仍未做：同一目标剩余量的跨 bar 结转（冻结契约无法表达，见 B 节 D-PARTIAL；现为执行 bar 截断 + 报告，策略重发目标即逐 bar 收敛）；bar 成交量由调用方另行提供（`PriceBar` 无成交量字段）；尚无 `plugins/` 下的生产 StrategyProvider（TSMOM 在 research/，须经 Promotion，H5）。
- **P6 / P10（W1）**：路由结果的身份只由 `run_hash` 绑定；切换成本在回测成本之外另计且不重设仓位；端到端测试中的 ACTIVE 生命周期只是测试夹具。
- **P9 校准**：检测器抛出异常时直接传播而不计为 INCONCLUSIVE；G5 未实际运行（开封消耗由 G0–G4 通过推断）；（控制台 `gate_calibration` 报告种类已补上）；8 个种子 × 2 天只是冒烟规模。
- **P8 稳健性**：~~CSCV 分块之间不做 purge~~（R17 已修）；回测适配器只验证单标的；状态标签由调用方提供，必须是因果的；
  `delay_stress_bars = 0` 或空 `time_alignment_offsets` 的 Profile 下 G4 永远不能 PASS（R14，是否允许豁免属 D-PFIELDS）。
- **P11 循环**：~~审计只在内存中~~ ✅ 已修（2026-09-26）：`LoopAuditLog(path)` 可选持久审计（哈希链、只追加、fsync，重放校验，篡改 / 截断拒绝），
  重启从最后一轮续跑、累计预算与停机状态、护栏对象随之恢复，从不重跑已记录轮次；轮中崩溃（只 started 未 recorded）→ 停止待人工审查；
  ~~算力秒数为阶段自报，不是实测~~ ✅ 已修（2026-09-26）：每次 `stage.run` 以单调墙钟 + CPU 时钟实测，放在哈希记录之外（`ResearchLoop.metrics` /
  `research_loop.metrics`），预算仍按 max(声明, 报告)，超出显式容差（无默认，未配置只报告）即标记（ADR-0049 实施说明 durable audit and measured compute）。
  ~~研究侧组合根未接持久审计（`ResearchMemory` 仍在内存）~~ ✅ 已修（2026-09-26）：`open_synthetic_loop(config, state_dir=...)` 把审计、TrialLedger、
  开封账本、谱系、失败登记、审阅队列（新增持久 `ReviewQueue(path)`）与每轮记忆检查点放在一个目录；重新打开时全部恢复并交叉校验（配置指纹、
  中断轮次、审计 ↔ 检查点、各文件位置、增量 ↔ 审计摘要、审计 ↔ 各账本），任一不符即拒绝启动（`LoopStateInconsistent`）；单个文件的尾部整行删除
  由跨文件位置发现（ADR-0049 实施说明 durable composition；`tests/research/loop/test_loop_durable.py`）。
  ~~重新打开时预算 / 开封额度未绑定（可用更大的预算或更多获准族续跑同一目录）~~ ✅ 已修（2026-09-26）：配置指纹绑定 `LoopBudget` 与完整
  `OosUnsealBudget`（额度、获准族、批准人）及精确节奏，任何变化拒绝（提高预算须新 `state_dir` / `loop_id`），机制侧续接审计也核对 `budget_hash`；
  ~~所有文件一致截回更早轮次边界无法发现~~ ✅ 可选外部锚点（`anchor=`，目录外 `FileAnchor` 或任一 `StateAnchor`）：落后 / 分叉 / 锚点丢失即拒绝；
  不给锚点时仍是已记录的限制（ADR-0049 实施说明 durable review fixes）。
  ~~总线只在内存中~~ ✅ 已修（2026-09-26）：`infrastructure/event_bus/FileEventBus(root)`——每主题哈希链只追加日志、每（消费者，主题）原子替换的
  offset / 确认集（绑定写入时的日志长度与头哈希），通过同一 bus contract suite，与内存总线差分等价（发布端不去重，与 Protocol 一致），
  重开后重放、未确认即重投，篡改 / 断链 / 半行 / 尾部删到消费者见过的长度以下 / 被编辑的状态一律拒绝，`fcntl.flock` 单写者锁
  （ADR-0044 Implementation note, file-backed bus；`tests/infrastructure/event_bus/test_file_event_bus.py`）；调用方可向持久组合传
  `bus=FileEventBus(state_dir / "bus")`，记录哈希不变（`test_loop_durable.py`）。仍未做：组合根在持久模式下自动使用 `state_dir/bus`
  并与审计交叉校验；最后一次写消费者状态之后追加、又被尾部删除的消息不可发现；NATS / Control Plane 持久化（D-10）。
  仍未做：最后一轮之后未被取用的人工审批不被检查点或锚点引用；
  审计记录与记忆检查点尚不是版本化契约；持久组合只有合成市场组合根。
- **P13 模拟执行**：仅模拟；无实盘场所、无密钥、无下单端点（结构上拒绝）。
- **数据集接线**：只支持点时刻模拟数据集（区间数据集被拒绝）；~~尚无 PostgreSQL 变体测试~~（✅ 已补：
  `tests/infrastructure/bars/test_dataset_bars_postgres.py` / `test_manifest_pair_postgres.py`，与 SQLite 侧同一套测试函数对象、
  同一套断言在真实 PostgreSQL 测试库上逐一通过，见 ADR-0037 实施说明）；与特征路径的证明逻辑部分重复；无缓存。
- **研究控制台**：前端单包约 1.19 MB；矩阵与路由纸面运行只有 API 与计数，没有专门页面。

## D. 调试阶段的顺序建议

1. 在最新 HEAD 上跑严格门禁（ruff / format / mypy / `uv lock --check` / 全量 pytest），修复任何失败。
2. 逐 Phase 跑端到端冒烟：合成市场 → 特征 → 状态 → 事件 → Outcome → 策略 → 回测 → 验证（G0–G4）→ 矩阵 → 路由 → 循环 → 模拟执行。
3. 处理 C 节缺口中不需要 Raphael 决定的部分；B 节等 Raphael 决定。
4. 在小规模真实数据集（≤ 2 万行）上重复第 2 步，只作能力验证，不形成任何市场结论（Profile 数值未冻结）。

## E. 真实数据冒烟发现（Real-data smoke findings）

来源：D 节第 4 步的能力冒烟 `tests/infrastructure/e2e/test_research_pipeline_real_data.py`（`postgres` 标记；PostgreSQL 测试 catalog + `tmp_path` warehouse）。
只验证管道能力（逐步哈希绑定、重跑哈希一致、无超出截止时刻的数据、判定属于已定义判定），**不形成任何市场结论**；验证用 TEST ONLY Profile，从不断言 PASS。

| # | 严重度 | 位置 | 发现 | 处理 |
|---|---|---|---|---|
| E0 | 记录 | `data/warehouse`（只读检查） | 本地 warehouse 只有表元数据（约 140K），没有真实行情；网络采集器未运行。冒烟因此沿用 G1 首切片方式：Binance 12 槽 kline 格式（BTCUSDT / ETHUSDT 各 300 根 1m）经真实入库路径（归档 D2、REST D3D/D3E、规范化 E1、D-33 对账、质量报告 E3、上市 E2、F3 构建）进入隔离 catalog | 真正的真实数据跑一遍需要 Raphael 授权运行采集器（网络），列为后续 |
| E1 | 中 | `infrastructure/bars/dataset.py` × `infrastructure/feature/dataset.py` | 同一条链需要**两个** manifest：P4 / P5 取 bar 只接受点时刻 spec，而 F4 在逐 bar 评估时刻需要区间 spec（点时刻 spec 只能在 `simulation_time` 之后评估）。两者只通过相同的 snapshot 绑定与相同的 OHLC 相互对应，没有任何对象把二者绑定为"同一数据集" | ✅ 已修（"数据集对"绑定）：`infrastructure/bars/pair.py` 的 `pair_manifests(builder, feature_manifest_hash, price_manifest_hash)` 只经验证型 `ManifestStore` 加载两份 manifest，证明特征侧为区间 `[start, end)`、价格侧为单点且 `simulation_time == end`、`knowledge_cutoff` 相同、上游 snapshot 相同、ADR-0032 选择与其余政策绑定相同、universe / 数据集表 / 数据窗口相同、价格侧成员 = 整个区间内都是成员的 episode（同一 end 时 listing revision，部分成员拒绝）、排除相同、价格 lineage ⊆ 特征 lineage（Canonical 表集合相同，证据缺口 ⊆，质量报告相同），返回不可变 `ManifestPair`（两哈希 + 规则哈希下的 pair 哈希）；冒烟改用它（收盘价一致性保留为推论检查）；区间 spec 取 bar 仍不支持；研究侧后续 ✅ 已修（2026-09-26）：`ValidatorSetup.manifest_pair` + `feature_manifest_hashes`（信号不带 manifest 哈希，由调用方显式传入特征请求的哈希），`G0.manifest_binding` 核对 pair 价格哈希 = `dataset_bars` 的 manifest、特征哈希 = 各特征请求的 manifest、pair 哈希可重算（`pair_hash_of`），pair 无 `dataset_bars` / 特征哈希无 pair 视为不一致；任一不符 FAIL；不给 pair 时 E5 不变，合成路径不变；冒烟改走 pair 并断言该门 PASS；回归测试 `tests/research/strategies/test_backtest_validation.py`（ADR-0041 Implementation note, manifest pair in G0）；回归测试 `tests/infrastructure/bars/test_manifest_pair.py`（ADR-0037 Implementation note E1） |
| E2 | 低 | `research/experiments/state_strategy.py`（P6） | 矩阵按权益点时间（= bar `interval_end`）归属收益，状态必须恰好在该网格上评估；ADR-0032 下 bar 收盘后 5 s 才可见，因此在该网格上所有特征 / 状态 / 策略输入都滞后一根 bar | 设计如此（因果、无未来函数）；在文档中说明，或允许矩阵按"t 时刻已知的最近状态"归属 |
| E3 | 中 | `research/validation/pipeline.py`（`build_report`） | `ValidationReport.created_at` 计入内容哈希，而 `PipelineBacktestValidator` 经 `build_report` 用墙钟时间戳 → 同一验证重跑报告哈希不同（循环 W2 靠事后 `model_copy` 补救） | ✅ 已修：`ValidationContext.created_at`（可选，默认不变）给定时作为报告时间戳（经校验）；回归测试 `test_a_context_stamp_makes_the_report_hash_reproducible` |
| E4 | 中 | `research/strategies/validation.py`、`research/validation/g4.py` | G4 输入只在 G0 – G3 未 FAIL 时惰性构建；本夹具 G2 FAIL（成本），经 `validate` 永远走不到 G4。构建 G4 输入的逻辑只在私有 `_robustness` 中，没有公开入口 | ✅ 已修：公开 `PipelineBacktestValidator.robustness_input(spec, backtest)`（重跑不复现则拒绝）与诊断模式 `robustness_diagnostic`（`diagnostic_report_only`，只报告、不改判定、不入册）；冒烟改用公开 API；回归测试 `test_the_public_g4_builder_is_what_validate_runs`、`test_a_diagnostic_g4_after_a_fail_is_report_only`（ADR-0041 Implementation note E4/E5） |
| E5 | 中 | `research/strategies/validation.py`（`ValidatorSetup.manifest_content_hash`） | 验证器构造 `OutcomeRequest` 时 manifest 哈希由调用方给出、按信任接受，并未证明试验 bar 就是该 manifest 的 bar；`BacktestRequest` 也没有 manifest 槽位（只在 `DatasetPriceBars` 旁路记录） | ✅ 已修：`ValidatorSetup.dataset_bars: DatasetPriceBars` 给出时新增适配器门 `G0.manifest_binding`（哈希一致、每根重跑 bar 属于包装、标的有 bar、不晚于 `price_cutoff`），不符判 FAIL（G0 → REJECTED / CONTRACT_VIOLATION）；`None` = 合成路径，视图标注 `synthetic_unverified`；冒烟传入 `DatasetPriceBars`；回归测试 `test_a_mismatched_manifest_hash_is_refused_at_g0`、`test_a_matching_manifest_passes_the_g0_binding_and_changes_nothing_else`、`test_bars_outside_the_manifest_are_refused`、`test_the_synthetic_path_is_labelled_unverified`（ADR-0041 Implementation note E4/E5） |
| E6 | 低 | `research/validation/stats.py`（`overlap-clusters`） | 每分钟决策 + 15 分钟标签窗口使全部标签连成一个重叠簇，有效样本数 = 1（G2 INCONCLUSIVE）。这是保守方法的正确结果，但意味着"持续持仓"类策略需要稀疏的决策节奏才能得到有效样本 | 研究设计问题（决策节奏 vs 标签窗口），非代码缺陷；记录 |
| E7 | 低 | `infrastructure/strategy/signals.py` | `signals_from_features` 给所有信号同一个 `knowledge_time`（调用方给出的单值），知识轴因此很粗；可见性只按 `available_time`，不影响因果 | 记录；需要逐值知识时间时再扩展 |
