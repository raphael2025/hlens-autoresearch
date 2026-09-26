# ADR-0054: 回测契约扩展——成交量上限剩余量跨 bar 结转（D-PARTIAL）

| 字段 | 值 |
|---|---|
| 状态 | Proposed (2026-09-26)，起草: Claude Code（Opus），待 Raphael 决定（红线） |
| 日期 | 2026-09-26 |
| 决策者 | **Raphael**（改冻结的回测契约 `core/contracts/strategy.py`，H1；调试待办 B 节原注"Codex 可在授权内批准"，按本次指示交 Raphael） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 5 回测；Phase 8 容量 / 成本压力 |
| 影响范围 | Contract（`PriceBar`、`BacktestProviderDescriptor`、`BacktestResult` 及其 `check_answers`，全部 additive）/ `plugins/backtest/` |
| 是否破坏兼容 | 否：新字段缺省时从哈希载荷中省略，既有请求 / 结果 / descriptor 哈希逐位不变（须以金值测试证明） |
| 前置 | [ADR-0017](0017-provider-delivery-schedule.md)、[ADR-0038](0038-strategy-risk-backtest-providers.md)（含 2026-09-26 execution realism 实施说明）、[ADR-0041](0041-validation-robustness.md) |

## 决策包（Decision packet）

- **问题**：是否扩展回测契约，使按成交量上限没成交完的剩余量可以顺延到同一标的之后的 bar 继续成交？
- **选项**：A. additive 扩展：新执行模型字面量、按模型放宽成交规则、结果增加剩余量字段、`PriceBar` 增加可选 `volume`；B. 不改：执行 bar 截断、取消剩余量并在 `ExecutionReport` 中报告，策略每根 bar 重发目标逐步到位。
- **推荐**：A。B 已能让仓位逐 bar 收敛，但需要策略配合；A 让"大单分多根 bar 成交"成为回测器本身可审计的语义，也为 G4 容量检查提供与执行一致的依据。
- **不决定时保持不变**：`next_bar_open` 是唯一执行模型；剩余量取消并报告；默认回测结果逐字节不变。

## 背景

ADR-0038 execution realism 说明已实现可选 `ExecutionModel`（参与率上限、平方根冲击、融资），但冻结契约无法表达结转：

1. `BacktestResult.check_answers` 要求每笔成交都在该目标的**执行 bar**（`execution_bar()`），参考价 = 该 bar 开盘价；
2. `len(fills) + unexecuted_targets <= len(targets)`——每个目标至多一笔成交；
3. `BacktestResult` 没有剩余量字段，`BacktestProviderDescriptor.execution_model` 只有 `Literal["next_bar_open"]`；
4. `PriceBar` 没有成交量，成交量只能经 `ExecutionModel.bar_volume` 旁路提供（`canonical.bars_1m` 实际有 `volume` 列）。

## 裁决（提案）

### 1. 新执行模型字面量

`BacktestProviderDescriptor.execution_model: Literal["next_bar_open", "next_bar_open_participation"]`。旧 descriptor 的值不变，
其哈希不变；导出 Schema 的 enum 增加一项。

`next_bar_open_participation` 的语义：

- 目标在执行 bar（定义同 `next_bar_open`）按执行前权益定量，得到**目标变化量**（以数量计，之后不再按权益重新定量）；
- 每根 bar 最多成交 `max_participation_rate × 该 bar volume`（参数属回测器，不属契约）；未成交部分为**剩余量**，在该标的的
  下一根 bar 开盘继续成交，参考价 = 那根 bar 的开盘价，成本与冲击逐笔计算；
- 剩余量在以下任一情形结束，并记录结束原因：全部成交（`filled`）；同一标的出现更晚的目标（`superseded`，新目标按**实际持仓**
  重新定量，旧剩余量取消）；数据结束（`end_of_data`）；
- 每笔成交的方向必须与目标变化量相同；同一目标各笔成交的数量绝对值之和不得超过目标变化量的绝对值。

### 2. 放宽的成交规则（只对新执行模型）

`check_answers` 按 descriptor 的 `execution_model` 分支；`next_bar_open` 分支**逐字不变**。新分支：

- 成交属于某个目标 ⇔ `(decision_time, instrument)` 命中该目标；
- `fill_time` 必须是该标的某根 bar 的 `interval_start`，且 `执行 bar ≤ fill_time < 同标的下一目标的执行 bar`（没有下一目标则不设上界）；
- 参考价 = 该 bar 开盘价；同一目标的成交按 `fill_time` 严格递增（一根 bar 至多一笔）；
- 计数：`有成交的目标数 + unexecuted_targets <= 目标数`；
- 每个目标的结转记录（§3）与其成交一致：`filled` 时剩余为 0，其余原因剩余为正。

### 3. 剩余量字段

`BacktestResult` 增加 `remainders: tuple[FillRemainder, ...] = ()`；`FillRemainder`（新模型）= `instrument`、`decision_time`、
`requested_quantity`、`filled_quantity`、`remaining_quantity`（`>= 0`）、`ended_by`（`filled` / `superseded` / `end_of_data`）、
`ended_at`（结束所在 bar 的 `interval_start`，`end_of_data` 为最后一根 bar）。只有新执行模型可以非空。
**哈希**：该字段为空时从 `_hashed_fields()` 与内容哈希载荷中省略，因此既有 `result_hash` 逐位不变。

### 4. `PriceBar.volume`（可选）

`PriceBar.volume: NonNegativeDecimal | None = None`，为 `None` 时从哈希载荷中省略——既有 `PriceBar`、`BacktestRequest` 的
`request_hash` 逐位不变。新执行模型要求其执行路径上的每根 bar 都有 `volume`，缺失即 `BacktestInputError`（不填补）。
`ExecutionModel.bar_volume` 旁路保留给 `next_bar_open` 的截断变体；两者同时给出且不一致时拒绝。
`infrastructure/bars/dataset.py` 可从 `canonical.bars_1m.volume` 填入。

**明示假设**：成交量是该 bar 收盘后才知道的量；它只被模拟器用来界定"这根 bar 市场能承载多少"，**永远不进入**策略或风控的
决策输入（`StrategyRequest` / `RiskRequest` 不含 `PriceBar`），因此不构成 C-L1 泄漏。这与 ADR-0038 现有截断变体、G4 容量检查的
参与率假设相同。

### 5. 版本

全部为 additive：一个 Literal 值、一个新模型、两个带缺省且缺省时省略出载荷的字段。按 02-domain §3 第 3 条属 minor；
信封版本的处理与 [ADR-0052](0052-validation-contract-completion.md) §4 相同（先盘点数据面重放，再决定是否随 2.1.0 一起发布）；
若 ADR-0052 不被接受，本 ADR 单独按同一规则处理。新执行模型的回测器 version 以 `1.2.0+exec.<fingerprint>` 形式绑定全部参数。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **A（推荐）** additive 扩展 | 结转是回测器可审计的语义；与容量检查一致；旧结果逐位不变 | 改冻结契约；`check_answers` 分支变复杂 | 推荐 |
| B 不改（重发目标） | 零契约变化；已实现 | 依赖策略每根 bar 重发；剩余量语义散落在策略中 | 默认 |
| C 把一个目标拆成多个合成目标 | 不放宽规则 | 伪造决策时刻，违背"目标即决策"的含义 | 拒绝 |
| D 新建一套回测契约（v2 模型） | 规则干净 | 与现有 Protocol / contract suite 重复，调用方全部迁移 | 不值得 |

## 后果

- 正面：大单分 bar 成交、剩余量去向可审计；冲击 / 容量估计与实际执行同源。
- 负面：新模型下买入持有闭式解不再成立（同现有截断变体），contract suite 需按执行模型区分检查项；剩余量不按权益重新定量是一个简化。
- 复现：既有请求 / 结果 / descriptor 哈希不变；新模型结果由参数指纹绑定。

## 实施计划（批准后）

| 模块 | 改动 |
|---|---|
| `core/contracts/strategy.py` | Literal、`FillRemainder`、`PriceBar.volume`、`BacktestResult.remainders`、按模型分支的 `check_answers`、缺省省略的哈希载荷 |
| `plugins/backtest/bar.py`、`execution.py` | 新执行路径；descriptor；`ExecutionReport` 与 `remainders` 一致 |
| `infrastructure/bars/dataset.py` | 可选填入 `volume` |
| `tests/contract_suites/backtest.py` | 按执行模型选择检查项 |
| `docs/architecture/05-plugin.md`、ADR-0038 实施说明 | 记录新模型 |

测试（先写失败用例）：9c0b851 金值 `result_hash` 与既有 `request_hash` 不变；`volume=None` / `remainders=()` 时哈希载荷不变；
分 bar 成交累计等于目标；`superseded` / `end_of_data` 记录；越出下一目标执行 bar 的成交、反向成交、超量成交、同 bar 两笔被
`check_answers` 拒绝；`next_bar_open` 分支对旧结果逐字不变；缺 `volume` 拒绝；改变 `t` 之后的 bar（含 volume）不改变 `t` 之前的输出；
四项工程检查。

## 合规检查

- [x] additive；缺省时既有哈希不变（待金值测试证明）
- [x] 不修改 Validation Constitution；不引入数值阈值（参与率属回测器参数，由调用方显式给出）
- [x] Domain 层仍无具体技术依赖；仍只是模拟，无下单能力
- [ ] 由 Raphael 本人批准——待定
