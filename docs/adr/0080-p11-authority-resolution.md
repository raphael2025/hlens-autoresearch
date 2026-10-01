# ADR-0080: Phase 11 ACTIVE、真实 source 与 metric 权威解析

| 字段 | 值 |
|---|---|
| 状态 | **Superseded by ADR-0098**（2026-09-30 PM 决定解除阻塞）；原 BLOCKED（2026-09-28；缺少可读取的当前生命周期权威记录、获准的真实数据源身份与 metric 计算定义）；正文“P12-LOOP 暂缓”部分由 ADR-0100 §7 修订 |
| 日期 | 2026-09-28 |
| 决策者 | Codex（依 Raphael 2026-09-28 授权审查；本文件不批准缺失的上游权威） |
| 起草者 | Codex |
| 相关 Phase | Phase 11（Continuous Research Loop） |
| 影响范围 | `research/loop/`、`research/operations/`；本轮不改实现与契约 |
| 是否破坏兼容 | 否 |

## 背景（Context）

本 ADR 对应 DP-P11-AUTHORITY / W2-P11：确定 ACTIVE 策略集合、非 synthetic source、metric 的权威来源与 provenance，之后才能让 loop 的非 synthetic 解析路径 fail closed。边界保持 ADR-0049 的外部调度、ADR-0074 的 synthetic-only operator 与 P12-LOOP 暂缓决定；不修改 `core/`、Constitution、Profile 数值或阈值。

现有代码没有可回答这些问题的权威数据源：

1. `core.lifecycle.strategy.LifecycleHistory` 是通过调用方传入的不可变值对象。`research.promotion` 校验并接收它，但不持久化后续状态变化。`infrastructure.registry.StrategyRegistry` 只追加 StrategyArtifact、equivalence 与 deployment 记录；StrategyArtifact 本身不含生命周期历史。因此无法从 Registry 得出某策略**当前**是否 ACTIVE，也无法枚举完整 ACTIVE 集合。
2. ADR-0067 的 degradation operation 只验证调用方提供的历史终态为 ACTIVE，并将该边界写入 evidence；其 `ObservationSource` / manifest 绑定调用方给出的 source id、hash、时间与方法身份，不解析 source 内容，也不认证来源或聚合。把它们直接当成 resolver 会推翻 ADR-0067 明示的边界。
3. 唯一真实数据 loop 入口 `research/loop/dataset_compose.py` 依赖 DatasetCatalog / DatasetBuilder；本任务禁止改 dataset 路径，DatasetBuilder API 也由另一任务修改。ADR-0074 的 operator 明确不调用该入口，仍为 synthetic-only。当前没有获准的非 synthetic operator 解析入口。
4. 数据集 manifest 能绑定数据快照内容，但当前项目决定没有指定哪些 dataset/source 是 P11 的真实权威、如何判定同一 source 的冲突 revision，也没有定义从原始观测到 Profile metric 的计算身份与精确聚合语义。不能按 Profile metric 名称自行挑选数据或发明均值 / 最新值规则。

## 决定（Decision）

**本 ADR 阻塞，不批准伪权威 resolver。** 只有 caller-declared LifecycleHistory、StrategyRegistry artifact 清单、caller-declared observation manifest 或“有 hash 的 dataset manifest”都不足以单独证明最新 ACTIVE 集合、真实 source 身份或 Profile metric 的权威值。哈希证明字节绑定，不证明最新性、来源真实性或计算正确性。

阻塞期间的运行规则：

1. 不解析、不缓存、不推断“当前 ACTIVE”集合。ADR-0067 既有一次性 degradation operation 只保留“调用方历史重放到 ACTIVE”的有限声明语义，不得被描述为当前 ACTIVE 权威查询。
2. 不将 source id / hash 或 metric manifest 视为已解析或已认证的真实行情来源；不从 synthetic、loop stage summary、backtest 或交易活动自动生成真实 metric。缺失、来源重复、内容 / 版本冲突或窗口证据冲突时，调用路径须拒绝且不写 degradation report。
3. ADR-0074 operator 继续只处理 synthetic provider；P11 保持外部调度，不引入仓内 scheduler。非 synthetic dataset loop 仍由显式调用的现有 dataset composition API 驱动，不代表其具有 ACTIVE / source / metric 权威解析能力。
4. 因上述缺口，本轮不新增 resolver 或接线，不改 `research/loop/dataset_source.py`、`dataset_compose.py`、`research/operations/degradation.py`、`core/`、ADR-0067 的既有报告语义。后续实现必须先以新 ADR 定义可读取的权威生命周期 head / ACTIVE 集合、source 身份及版本冲突规则、metric 输入与计算方法身份，并指定非 synthetic loop 的解析入口；实现应记录各 authority ref、content hash、snapshot/head identity、as-of 时间和拒绝原因。

本决定不改变 synthetic-only operator 行为、不创建运行配置、不选择或生成 Validation Profile，也不改变外部调度及 P12-LOOP 暂缓状态。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 将调用方生命周期历史视作当前权威 | 可立即复用 ADR-0067 输入 | 无法发现历史过期、遗漏或多个 ACTIVE；与 ADR-0067 的诚实边界冲突 | 拒绝 |
| 将 StrategyRegistry 中的 artifact 视为 ACTIVE 集合 | 有 append-only journal 与可选外锚 | Registry 没有 lifecycle transition；artifact 可对应 PAPER / PRODUCTION_CANDIDATE / ACTIVE，且不能记录后续 DEGRADED / RETIRED | 拒绝 |
| 对 caller manifest 的 source / metric 值直接作权威 | 可复用已有内容 hash | hash 不证明 source 身份、revision 最新性、聚合过程；无法定义冲突选择或精确 metric 算法 | 拒绝 |
| 由本轮新增本地 registry 自行充当权威 | 能定义可重放的本地存储 | 会新造 lifecycle 写入者、授权规则、更新语义与数据来源，超出已有决定；自身 append-only 不构成上游事实真实性 | 未批准 |

## 后果（Consequences）

- 正面：不把历史声明、文件存在或内容哈希误标成“当前权威”；既有 synthetic-only、外部调度与 fail-closed 边界维持不变。
- 负面 / 代价：W2-P11 resolver 与真实数据路径接线无法完成；非 synthetic degradation 只能继续通过 ADR-0067 的显式声明输入运行，不能声称 source 或 ACTIVE 已权威解析。
- 需要迁移的内容：无。
- 对复现性的影响：现有 loop、manifest 与 report hash 不变；不产生新的真实运行身份。

## 重开所需输入

以下均须明确来源后才能批准 resolver ADR：

1. 提供或批准 ACTIVE 生命周期历史的可读取权威 head / registry，以及追加、排序、回滚检测、并发写入和完整 ACTIVE 枚举规则；说明 `ACTIVE → DEGRADED / RETIRED` 后如何从集合中移除。
2. 选择真实数据 source / dataset authority 与 source 身份、revision、可用时间、冲突 head 的拒绝规则；确认它如何与 DatasetBuilder / DatasetCatalog 路径配合。
3. 给出每个可监控 metric 对应的观测字段、单位、精确算法 / 版本、窗口与缺失 / 多样本处理规则，并定义来源记录及 resolver provenance。
4. 指定 dataset loop 的获准解析入口 / 调用方，并确认其不能触发 scheduler、不能突破 ADR-0074 synthetic-only operator。

在上述决定到位前，所有 resolver 需求必须 fail closed；不得用测试或 synthetic 数据填充真实运行配置。

## 合规检查

- [x] 不修改冻结 Contract、Domain、Schema、Constitution、Profile 数值或验证阈值
- [x] 不改变 ADR-0074 synthetic-only operator、ADR-0049 外部调度或 P12-LOOP 暂缓
- [x] 不触及本任务排除的 dataset 路径
- [x] 缺少权威或遇到冲突时不猜测、不写报告

## 参考

- [ADR-0049：Continuous Research Loop](0049-continuous-research-loop.md)
- [ADR-0062：Validation Profile freeze registry](0062-profile-freeze-registry.md)
- [ADR-0067：P11 degradation evidence operator](0067-p11-degradation-evidence-operator.md)
- [ADR-0074：Synthetic loop operator](0074-synthetic-loop-operator.md)
- [模块基础逻辑计划：DP-P11-AUTHORITY](../plans/2026-09-28-module-foundation-completion.md)
- `core/lifecycle/strategy.py`、`research/promotion/service.py`、`infrastructure/registry/registry.py`、`research/loop/dataset_compose.py`
