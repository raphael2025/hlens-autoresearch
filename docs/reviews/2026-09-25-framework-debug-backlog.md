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
| R8 | 中 | `apps/worker/loop.py` | 阶段实际用量超出预估时，事后才发现（超支一次后停机） | 🔨 W2 调试中：记录超支量、失败阶段按实际用量计费 |
| R9 | 中 | `research/loop/stages.py` | 循环审计记录的哈希含浮点数（跨平台可能分叉） | 🔨 W2 调试中：固定精度 Decimal 字符串 |
| R10 | 低 | `research/loop/memory.py` | LLM 草稿审阅无身份门禁 | 🔨 W2 调试中：必须给出非循环自身的审阅人并记录 |
| R11 | 低~中 | `infrastructure/strategy/signals.py`、`infrastructure/event/inputs.py` | `available_time = evaluation_time` 依赖上游契约保证因果 | 设计如此（F4 / P2 契约已保证 `latest_input_available_time <= evaluation_time`）；调试时加跨层断言 |
| R12 | 低~中 | `infrastructure/event/runner.py` | 稀疏检查点网格下，检查点之间的回填对执行器不可见 | 已在 ADR-0036 记录；调试时评估默认网格 |

## B. 需要 Raphael 决定（红线，Claude 不自行决定）

| ID | 问题 | 推荐 | 不决定时 |
|---|---|---|---|
| D-FLOAT | `GateResult`、Validation Profile 阈值等核心域模型在哈希载荷中使用浮点数（改为 Decimal 属破坏性契约变更，H1） | 另起 ADR：新 major 或新增 Decimal 字段并弃用浮点字段 | 保持现状：同平台可复现，跨平台不保证（ADR 已如实记录） |
| D-PFIELDS | Validation Profile 缺少容量、跨资产一致性、CSCV 分块数、开封预算、冲击系数等字段（改 Profile 结构属 H2） | 另起 ADR 增加这些字段（数值仍待校准后冻结） | 这些规则只能由调用方显式传参；未传时判为 INCONCLUSIVE，绝不判通过 |
| D-CTRL | 校准证据（P9 × P8）：`significance.multiple_testing_threshold` 被两处反向使用——G3 要求校正后 p ≤ 阈值，G1 负对照要求对照 p ≥ 阈值；因此"放宽"阈值反而让负对照全部失败，Profile 无法单独调节两者 | 与 D-PFIELDS 合并起 ADR：为负对照增加独立字段 | 保持：两者共用一个阈值（偏保守，不会多放行） |
| D-MINEFF | P6 条件假设登记要求 `minimum_effect`（测试中用 `"0.1"`）：它是假设字段还是验证阈值？ | 视为预登记的假设字段（研究者声明），不作为验证门槛 | 测试用值只存在于测试中；生产路径不设默认 |

## C. 各批次自报的已知缺口（调试时逐个处理）

- **P3 事件**：一次请求只覆盖一个标的；检查点网格代价约为检查点数 × 可见输入数；交互规格声明的上游只做形式检查；尚无物理 Event 表。
- **P4 Outcome / 最小验证门**：负对照为单次固定种子；开封记录与 Outcome 表只在内存中；统计用浮点正态近似。
- **P5 策略 / 回测**：执行模型单一（下一根开盘成交、无部分成交 / 融资 / 冲击）；尚无 `plugins/` 下的生产 StrategyProvider（TSMOM 在 research/，须经 Promotion，H5）。
- **P6 / P10（W1）**：路由结果的身份只由 `run_hash` 绑定；切换成本在回测成本之外另计且不重设仓位；端到端测试中的 ACTIVE 生命周期只是测试夹具。
- **P9 校准**：检测器抛出异常时直接传播而不计为 INCONCLUSIVE；G5 未实际运行（开封消耗由 G0–G4 通过推断）；（控制台 `gate_calibration` 报告种类已补上）；8 个种子 × 2 天只是冒烟规模。
- **P8 稳健性**：CSCV 分块之间不做 purge；回测适配器只验证单标的；状态标签由调用方提供，必须是因果的。
- **P11 循环**：总线与审计只在内存中（NATS / Control Plane 持久化待做）；算力秒数为阶段自报，不是实测；审计记录尚不是版本化契约。W2 正在把状态、验证、实验与进化阶段换成真实组件。
- **P13 模拟执行**：仅模拟；无实盘场所、无密钥、无下单端点（结构上拒绝）。
- **数据集接线**：只支持点时刻模拟数据集（区间数据集被拒绝）；尚无 PostgreSQL 变体测试；与特征路径的证明逻辑部分重复；无缓存。
- **研究控制台**：前端单包约 1.19 MB；矩阵与路由纸面运行只有 API 与计数，没有专门页面。

## D. 调试阶段的顺序建议

1. 在最新 HEAD 上跑严格门禁（ruff / format / mypy / `uv lock --check` / 全量 pytest），修复任何失败。
2. 逐 Phase 跑端到端冒烟：合成市场 → 特征 → 状态 → 事件 → Outcome → 策略 → 回测 → 验证（G0–G4）→ 矩阵 → 路由 → 循环 → 模拟执行。
3. 处理 C 节缺口中不需要 Raphael 决定的部分；B 节等 Raphael 决定。
4. 在小规模真实数据集（≤ 2 万行）上重复第 2 步，只作能力验证，不形成任何市场结论（Profile 数值未冻结）。
