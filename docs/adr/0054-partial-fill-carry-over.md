# ADR-0054: 回测契约扩展——成交量上限剩余量跨 bar 结转（D-PARTIAL）

| 字段 | 值 |
|---|---|
| 状态 | Accepted (2026-09-26)，决策者: Raphael（"同意"），起草: Claude Code（Opus）；**re-declared at 2.1.0 (2026-09-26)**（见文末 Implementation note — 2.1.0）；CODE_COMPLETE / DEBUG_PENDING |
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

## Implementation note (carry-over, 2026-09-26)

实施批次（Claude Code，Opus）。**裁决不变**，按 §1 – §5 实施；状态 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

- **契约**（`core/contracts/strategy.py`，全部 additive）：`BacktestProviderDescriptor.execution_model` 为
  `Literal["next_bar_open", "next_bar_open_participation"]`；新模型 `FillRemainder`（登记表末尾追加，Schema 134 → 135）；
  `PriceBar.volume: NonNegativeDecimal | None = None` 与 `BacktestResult.remainders: tuple[FillRemainder, ...] = ()` 用 Pydantic
  `Field(exclude_if=...)` 在缺省时从 `model_dump` 中省略（嵌套 dump 同样省略），因此 `content_hash`、`_hashed_fields()` 与导出载荷
  对旧对象逐位不变。`FillRemainder` 构造时自证：目标变化量非零、成交同向且绝对值不超量、`remaining = |requested| − |filled|`（120 位精确）、
  `filled ⇔ remaining = 0`、`ended_at >= decision_time`；`remainders` 按 `(decision_time, instrument)` 唯一升序。
- **`check_answers`** 按 descriptor 分支。`next_bar_open` 分支原有检查逐字不变，只在前面加一条"不得有结转记录"（旧结果恒为空，行为不变）。
  `next_bar_open_participation` 分支（`_check_carry_over`）实现 §2 全部规则，并补充三条使记录可审计的规则：有成交的目标必须有记录；
  记录必须对应一个有执行 bar 的目标；`ended_at` 必须与结束原因一致（`filled` = 最后一笔成交的 bar，`superseded` = 同标的下一目标的执行 bar，
  `end_of_data` = 该标的最后一根 bar 且没有下一目标的执行 bar）。
- **回测器**（`plugins/backtest/`）：`ExecutionModel(carry_over=True, max_participation_rate=...)` 开启新模型（`carry_over` 必须是 `bool`，
  且需要参与上限；关闭时指纹载荷不含该键，既有指纹逐位不变），`BarBacktester` 随之声明 `next_bar_open_participation`、version
  `1.2.0+exec.<fingerprint>`（`CARRY_OVER_VERSION`）。语义取舍：
  - 每个**已定量**且目标变化量非零的目标都有一条记录（含一次成交完的，`filled`）；变化量为 0 的目标计为已执行、无记录；在执行 bar 之前就被
    取代的目标从未定量，计为未执行、无记录（与 v1 相同）；有记录但一笔未成交（成交量为 0）的目标计为未执行；
  - 成交量只读 `PriceBar.volume`（结转路径上缺失即 `BacktestInputError`，旁路 `bar_volume` 不能替代）；旁路与 `PriceBar.volume` 同时给出
    且不一致即拒绝——截断变体同样执行这条检查（只在 bar 带 `volume` 时触发，既有输入不受影响）；
  - 冲击与融资照旧逐笔 / 逐 bar 步计算；剩余量记账在 120 位、陷阱 `Inexact` 的上下文中进行（会舍入即拒绝），因此 `filled` 目标的各笔成交之和
    精确等于目标变化量；
  - `ExecutionReport.carried` 恒等于 `BacktestResult.remainders`；`ExecutionReport.remainders`（截断时取消的剩余量）在新模型下为空。
- **版本（§5，按 ADR-0052 §4 先盘点）**：盘点结论——数据面规范化把 `CONTRACT_SCHEMA_VERSION` 写入每一行 Canonical revision
  （`infrastructure/canonical/rules.py` `contract_schema_version` 列），重放时 `infrastructure/canonical/normalizer.py` `_exact` 逐列比较
  "已提交行"与"重新规范化的行"，不一致即 `CatalogIntegrityError`。因此把信封升到 2.1.0 会让已提交的 D-NET 数据（2.0.0 行）无法幂等重放；
  同时新构造对象的 `request_hash` / `result_hash` 都会改变（`request_hash` 含信封版本）。按 §5 / ADR-0052 §4，本批次**不升信封**、不自行
  选择，`CONTRACT_SCHEMA_VERSION` 保持 `2.0.0`（与 ADR-0023 / 0024 / 0038 新增 additive 模型时的做法相同），并把"是否 / 如何随 2.1.0 发布"
  列为 `ARCHITECTURE_DECISION_REQUIRED`（与 ADR-0052 的信封决定是同一个问题，交 Codex / Raphael）。在该决定之前：新字段缺省即省略，
  旧载荷、旧哈希与数据面重放全部不受影响（下列金值测试证明）；已知代价是 2.0.0 旧实现读到带新字段的载荷会按 `extra="forbid"` 拒绝（fail closed）。
- **测试**：`tests/plugins/backtest/test_carry_over.py`（50a43a4 记录的金值：黄金请求的 `request_hash`、首根 bar 哈希、默认 descriptor 哈希、
  两个截断变体的 `(fingerprint, provider_hash, result_hash)`，加上 `test_execution_model.py` 中 9c0b851 的 v1 `result_hash`；分 bar 成交之和
  等于目标变化量；不按权益重新定量；零成交量等待；`superseded` 与按实际持仓重新定量；执行 bar 前被取代的目标；`end_of_data`；多标的；
  缺 `volume` 拒绝；旁路不一致拒绝；改变 `t` 之后的价格与成交量不改变 `t` 之前的成交 / 记录 / 权益 / 融资；14 种伪造结果被 `check_answers`
  拒绝：无对应目标的成交、越出下一目标执行 bar、bar 之间、同 bar 两笔、反向、超量、参考价、缺记录、无目标记录、数量不符、结束 bar / 原因不符、计数超额；
  `next_bar_open` descriptor 拒绝结转记录）；`tests/contract_suites/backtest.py` 按声明的执行模型选择检查项
  （`BacktestProviderContract` 的 descriptor 检查只接受 `next_bar_open`，新增 `CarryOverBacktestProviderContract` / `CARRY_OVER_CHECKS`），
  新变体（含冲击 + 融资）通过；`tests/test_strategy_contracts.py`（`FillRemainder` 不变量、`volume` 省略与校验、descriptor 字面量、
  `remainders` 排序与省略）；`tests/research/strategies/test_backtest_validation.py`（同参数的结转 / 截断变体是两个 provider，
  `G0.execution_model` 拒绝不符者、接受相符的结转回测且 `G0.reproducibility` 通过）。
- **未做（留给调试批次）**：`infrastructure/bars/dataset.py` 尚未从 `canonical.bars_1m.volume` 填入 `PriceBar.volume`（真实数据上新模型因此
  会以缺 `volume` 拒绝）；`exclude_if` 需要 Pydantic ≥ 2.12，`pyproject.toml` 仍写 `pydantic>=2.9`（锁文件为 2.13.5，未改依赖声明）；
  G4 容量检查未改读结转结果；剩余量不按权益重新定量是 §1 明示的简化。

## Implementation note — re-declared at 2.1.0 (2026-09-26)

分支 `core/adr-0052-into-full-code`（合入 ADR-0052 版本化重放 M1 ~ M3 之后）。取代上文实施说明"版本（§5）"一条中"保持 2.0.0"的选择
（该条保留为历史；其 `ARCHITECTURE_DECISION_REQUIRED` 已由 Codex K3 与 ADR-0052 版本化重放解决）：

- `PriceBar._FIELDS_SINCE = {"volume": "2.1.0"}`、`BacktestResult._FIELDS_SINCE = {"remainders": "2.1.0"}`（空元组 = 省略 = 不存在）、
  `FillRemainder._MODEL_SINCE = "2.1.0"`、`BacktestProviderDescriptor._VALUES_SINCE`（`execution_model="next_bar_open_participation"`
  自 2.1.0）；常量 `ADR_0054_VERSION`。2.0.0 信封携带任一项即拒绝；新对象为 2.1.0。
- 机制补充（`core/domain/base.py`）：字段"存在"= 出现在载荷中（非 `None` 且未被 `exclude_if` 省略）；新增 `_VALUES_SINCE`
  （已有字段的新取值自某版本起）。对 ADR-0052 的字段行为不变。
- 不带新内容的 2.0.0 请求 / 结果 / descriptor 按记录版本读取、哈希逐位不变，并仍通过 `check_answers`。
- 测试：`tests/test_adr_0054_0057_versions.py`；`tests/plugins/backtest/test_carry_over.py` 与 `test_execution_model.py` 的 50a43a4 / 9c0b851
  金值改为在 2.0.0 构造作用域内复核，金值未改。

## Implementation note — dataset volume mapping (B61, 2026-09-27)

状态 **CODE_COMPLETE / DEBUG_PENDING**（上文"未做（留给调试批次）"的第一项）。无 core / 契约 / Schema / 版本 / 哈希规则变化。

- `infrastructure/bars/dataset.py::backtest_bars_from_dataset` 生成的每个 `PriceBar` 现在带 `volume`：取自同一经 manifest / PIT 证明、
  经 lineage 与键 / 事件时间核对的已选 revision 的 `FeatureObservation.values["volume"]`（`BAR_VALUE_COLUMNS` 已包含它；不增加 catalog 查询），
  即 Canonical `bars_1m.volume`（`decimal(38, 18)`）的 `Decimal` 原值——不推断、不补零、不经 float。值不是 `Decimal`（或缺失）→
  `CatalogIntegrityError`，与 OHLC 相同（fail closed；两条路径共用同一个已证明 bar，因此 outcome 请求同样拒绝这种行）。
  `OutcomePriceBar` 没有 volume 字段，不添加。
- 哈希：`PriceBar.volume = None` 仍省略，既有空值载荷与哈希逐位不变——测试把数据集回测请求去掉 volume 后逐位复现 B61 之前（`dfa432b`）
  在两个独立构造的世界中算得的 `request_hash` `e07c52d9…`；带真实非空 volume 的数据集回测 `request_hash` 因此改变（预期，volume 是 bar 的一部分），
  改变某根 bar 的 volume 即改变请求哈希。默认 `next_bar_open` 执行模型不读 volume：成交与权益路径不变，只有 `request_hash` / `result_hash` 变。
  没有被钉住的数据集回测哈希，也没有重写任何已提交快照。
- 影响：结转模型（`next_bar_open_participation`）在真实数据集 bar 上不再因缺 `volume` 被拒；同时给出 `ExecutionModel.bar_volume` 旁路且与数据集
  volume 不一致时，按本 ADR 既有规则拒绝。
- 测试（SQLite / 真实存储 fixture，`tests/infrastructure/bars/test_dataset_bars.py`）：精确 `Decimal` 映射（与所选行同位数 / 指数）；两个独立世界
  构造结果相同；volume 进入请求哈希、去掉后复现旧哈希；outcome 路径无 volume；缺失 / int / 非数值文本 → `CatalogIntegrityError`。
  在 B61 之前的 `dataset.py` 上 7 项新测试中 6 项失败（outcome 不变量两边都通过）。PostgreSQL 标记的数据集 / 端到端测试未运行（本机未为本批次授权运行）。
- 上文其余未做项不变：G4 容量检查未改读结转结果；剩余量不按权益重新定量（§1 简化）。`pyproject.toml` 的 Pydantic 下限已由 B55 提到 ≥ 2.12。
