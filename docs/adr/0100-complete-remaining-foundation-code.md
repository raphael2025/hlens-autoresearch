# ADR-0100: 补完剩余底层代码（Raphael 2026-09-30 直接指令）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-30） |
| 日期 | 2026-09-30 |
| 决策者 | Raphael（直接指令：「你不管文档怎么说 能写的都写了」）；Claude Code 记录并细化 |
| 起草者 | Claude Code |
| 相关 Phase | Phase 1、7、11、12 |
| 影响范围 | `research/hypotheses/`、`plugins/`、`research/operations/`、`research/loop/`、`research/evolution/`、`infrastructure/universe/`、E1 路径；`core/` 仅 additive |
| 是否破坏兼容 | 否（新能力一律 additive；运行开关默认关闭） |

## 背景（Context）

2026-09-30 的代码补全轮之后，仍有一批代码因 ADR 中的“OPEN / 暂缓 / 待决定”而没有写。Raphael 直接指示把能写的全部写出来。项目的最终决策者是 Raphael，本 ADR 记录该指令，并给出实现边界。

## 决策（Decision）

**不变的底线**（研究诚信与资金安全，任何指令都不改变）：H3 / H4 / H6；实盘默认关闭，不连接实盘账户、不下单（H10、ADR-0084）；不猜 Validation Profile 数值；新运行能力默认关闭，必须显式开启。

在此之内，以下全部实现：

1. **P7 算子执行**：为 ADR-0082 / 0088 / 0099 已定义语义的全部算子提供执行 Provider（feature transformation、interaction、temporal event、conditioned / ensemble / negated strategy），严格按已接受语义实现。只使用 `available_time ≤ t` 的数据，缺值时传播缺失。`compile_plan` 通过显式注册的 Provider allowlist 编译计划；`TypedPlan.runnable` 仅在调用方显式启用（配置开关，默认关闭）且计划全部节点都有已注册 Provider 时为 True。试验计数与 admission 规则不变。
2. **横截面 rank / quantile**：新增 `rank_cs` / `quantile_cs` transform。统计总体为显式 universe 快照（钉定的 Universe / Dataset 引用）在同一 bar 时刻的全部成员；时间对齐按 bar 的 `interval_end`；缺值成员不计入总体；平局取平均秩。需要的契约字段以 additive 方式加入（契约 minor 版本升级）。
3. **P11 监控指标扩展**：闭集中加入所有能以“与基线相同的 validation 函数、相同参数，作用于近期窗口数据”计算的 metric；需要 Profile 固定窗口或参数族重跑的 metric，在近期窗口内按同一函数、同一参数重算，并在 metric definition 中记录窗口口径。任何无法做到同源同参的 metric 仍拒绝。**不发明新公式。**
4. **P11 默认运行环境**：提供基于 `Settings` 的默认 `AuthorityEnvironment` factory（Iceberg DatasetCatalog、插件注册表中的 BacktestProvider、按基线 StrategySpec 组装的决策管线），可以通过 `--authority-environment` 指定。
5. **ADR-0051 上市政策表**：联网核实 BTCUSDT / ETHUSDT 的 Binance 最早 1m 归档日与 `exchangeInfo`，作为新的政策版本写入 `POLICY_TABLE`，并附来源 URL 与核实时间；核实不到的标的不写。
6. **E1 有界化**：对 E1 路径中仍随 N / batch 增长的持有项（完整结果 tuple、`ParsedArchive` 整表、metadata 等）按静态分析逐项改为流式或有界。32 MiB 门槛不变，是否通过仍以实测为准。
7. **P12 循环内提案**：在研究循环中增加可选的替换提案触发（默认关闭），且只能使用独立、预先登记的密封评估窗口；循环不得复用已开封窗口；OOS → PAPER 仍须人工批准。

## 后果（Consequences）

- 正面：ADR 中标注为 OPEN / 暂缓的代码全部有实现。
- 负面 / 代价：大量新代码未经测试；运行开关打开前必须完成验收。
- 对复现性的影响：既有计划、报告、manifest 哈希不变；新能力带新版本标识。

## 合规检查

- [x] 冻结契约只做 additive 变更
- [x] 不修改 Validation Constitution / Profile 数值
- [x] Domain 层仍无具体技术依赖
- [x] 实盘保持关闭；新运行能力默认关闭

## 修订 1（2026-09-30）：P7 temporal 语义细化（计划格式 1.3.0 起）

实现审查发现 ADR-0082 第三次接受记录中的 temporal 输出规则与 Event 契约重复计入可观测滞后。上游 `event_time` 本身已是可观测时刻（`event_time = 最晚输入时刻 + observable_lag`）。决定如下：

1. **1.3.0 起**，lowered temporal EventSpec 的 `observable_lag = 0`，可见时刻 = 第二事件的可观测时刻（trigger `visibility` 不变）。1.2.0 计划的 lowering 输出保持原样（不改变其含义）。
2. 窗口按**发生时刻**度量：两侧都用 `event_time − upstream.observable_lag` 比较，第二事件的发生时刻须落在 `(first, first + N bars]` 内。
3. 两个输入必须是不同的 EventSpec（ref 不同），否则拒绝。
4. 1.3.0 temporal trigger 绑定上游哈希：`first_event_hash` / `second_event_hash`，与 `infrastructure/event/upstream.verify_interaction` 的约定一致；1.2.0 的无哈希规格仍由 runner 拒绝（fail closed）。

## 修订 2（2026-09-30）：基线运行记录 P11 同源重算所需输入

诚信加固后，P11 authority 要求近期指标所用的输入与基线**逐项相等**。但基线运行没有记录其中若干项（决策步长 / 预热期、初始权益、各门所用 seed、族试验计数、CSCV 分区数、冲击系数、状态标注器身份），因此现在一律以 `execution_unrecorded` / `baseline_input_unrecorded` 拒绝。决定：

1. 自本修订起，**新的**实验运行在复现元组中完整记录上述输入。优先使用现有的可扩展字段（如 repro params），不改 `core/`；确需改契约时只做 additive，契约升 minor 版本，并列出本地需要重新生成的 schema。
2. 记录值进入运行的内容哈希；P11 resolver 优先读取这些记录值做逐项核对。
3. **旧运行不回填、不推断**：缺记录的基线继续拒绝（H3 / H6）。
