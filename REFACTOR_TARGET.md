# REFACTOR_TARGET.md — HLENS 重构主策略规范

> 状态：**Accepted**（2026-10-03，Raphael 确认；ADR-0111）。后续开发与重构的最高指导文档；红线与范围的变更需 Raphael 批准。
> 依据：2026-10-03 三路只读审计（文档与授权 / 契约与数据管线 / 策略逻辑与代码结构）。
> 与其它文档冲突时以本文件为准；本文件取代的旧决定见 §7。

## 0. 审计结论（决定本规范形状的事实）

- 代码约 14.9 万行（不含测试 14.8 万行），其中 Phase 2~14 约 7.2 万行，全部未验收；Phase 1 数据平面自身约 6.4 万行。
- 仓库内真实行情只有 BTC / ETH 两天的 1m K 线；**aggTrades 从未进入 Canonical 层**；没有任何入口能对真实落盘数据跑通“读入 → 特征 → 回测 → 指标”。
- 全仓没有 numpy / polars / duckdb；特征、状态、策略、回测都是逐行 `Decimal`，feature / state / event runner 是 O(T²) 的 Pydantic 重校验。**这是首要瓶颈。**
- 没有发现“硬编码数值阈值的 Regime if-else 链”：阈值都是 Spec 参数。真正的风险是 Regime 标签到仓位的**硬切换**（`research/strategies/composite.py`、`research/router/router.py`）。
- 148 份 Schema 中严格未用的只有 2 份；约 95 份属于“Phase 1 用不到”，不是“没人用”。
- Iceberg：项目已在用私有 API 重写 PyIceberg 读写内核并锁死 0.12.0；D-META-AGE 仍开放并阻断历史回填。

## 1. 红线（任何授权、任何重构都不改变）

| # | 红线 |
|---|---|
| R1 | **PIT**：任何计算在时刻 t 只能使用 `available_time ≤ t` 的行；Outcome（未来收益）永不作为输入。 |
| R2 | **决策与成交分离**：t 时刻的决策最早在下一根 bar 开盘成交；成本（手续费、滑点）始终计入。 |
| R3 | **研究诚信**：不为提高回测表现修改验证规则、成本、切分、样本外区间、指标定义（H3）；不削弱测试（H4）；不删除失败实验与运行记录（H6）。 |
| R4 | **预登记**：成本模型、数据切分、指标与全部自由参数在看到结果之前写入 spec；每次运行计入试验次数。 |
| R5 | **可复现**：同一（git commit、dataset_id、spec 哈希、依赖版本）必须得到按位一致的结果。 |
| R6 | **研究与生产隔离**：研究代码不直接成为生产代码；`apps/` 运行时不 import `research/`。 |
| R7 | **无实盘**：不下单、不连接实盘账户、不使用交易凭据；仓库不含密钥、账户信息与行情原始数据。 |
| R8 | **数据不可变**：已发布的数据文件只追加、不改写；更正 = 新文件 + 新 manifest。旧项目与外部数据不动。 |

研究宪法（`docs/research/constitution.md`）的 A1~A6、C-L、C-S、C-T、C-P 条款继续有效；依赖 Profile 数值的条款（C-A7 / A8、C-R、C-G）随 Phase 4+ 一并冻结。

## 2. 物理隔离方案

**原则**：归档 = `git mv` 到 `archive/`，保留历史、不删除；`archive/` 不参与 pytest / ruff / mypy，任何保留代码不得 import 它。

| 归档到 `archive/` | 内容 | 原 Phase |
|---|---|---|
| `research/` 除 `sandbox/` 外全部 | states、events、outcomes、validation、strategies、hypotheses、experiments、router、synthetic_lab、loop、operations、evolution、promotion、reports、persistence | 2~12 |
| `plugins/` | states、events、outcomes、knowledge、llm、synthetic；features 中除 `bars.py` 外的模块；`backtest/reference.py` | 0.5、2~9、14 |
| `infrastructure/` | state、event、registry、event_bus、migration、content、plugins；`tools/registry_audit.py` | 2~14 |
| `apps/` | api、execution、promotion、web；worker 中的 loop、degradation、metrics | 8~13 |
| `tests/` | 上述模块对应的测试，同路径镜像迁入 | — |

**原地冻结（不移动、不再投入）**：Phase 1 的 Iceberg 数据平面（`infrastructure/` 的 catalog、revision、canonical、pit、universe、quality、dataset、streaming、feature、bars）。切片验收后另立 ADR 决定归档或保留为写入侧。E1-CAP-1 的 K 轴预填充与 D-META-AGE 随之冻结。

**继续复用**：`infrastructure/collector/binance_archive.py`（下载 + CHECKSUM）、`infrastructure/parser/binance_archive.py`（时间单位与覆盖区间校验）、`infrastructure/storage/local.py`（原子发布）。

**Schema**：`core/` 与 `schemas/` 第一阶段不移动（148 份导出与注册表由契约测试绑定）。
- 严格未用（2 份）：`RepresentationSpec`、`ExecutionModeChange`。
- 阶段外（约 95 份，冻结、切片不得 import）：research、validation_profile、profile_selection、lifecycle、artifact、loop_audit、cost_model、knowledge、llm、event_bus、state、event、outcome、strategy、synthetic 各组，以及 catalog DTO。
- 热路径禁用（逐行对象）：`RevisionRecord`、`ObservationTimes`、`AvailabilityDecision` 及各 Provider 的逐观测 / 逐 bar 模型。

**移动时必须同步修改**：`tests/test_architecture_boundaries.py`（断言目录存在的 4 处）、`tests/test_docs_consistency.py`（排除 `archive/`）、`pyproject.toml`（entry-points、mypy `files`、ruff 排除）、`plugins/features/__init__.py`、`apps/worker/__init__.py`。

## 3. 数据层降级标准（DuckDB / Polars 直连 Parquet）

**布局**（`data/` 已被 Git 忽略）：

```
data/lake/binance/spot/<table>/symbol=<SYMBOL>/date=<YYYY-MM-DD>/part-0.parquet
data/lake/binance/spot/<table>/manifest-<dataset_id 前 12 位>.json
```

**表**：`bars_1m`（open_time、close_time、open、high、low、close、volume、quote_volume、trade_count、taker_buy_volume、available_time）；`agg_trades`（agg_trade_id、price、quantity、first_trade_id、last_trade_id、transact_time、is_buyer_maker、available_time）。时间列为 UTC `timestamp[us]`，价格与数量为 `float64`，文件内按时间升序，zstd 压缩。

**manifest**：逐文件记录路径、sha256、行数、时间范围、来源 zip 的 CHECKSUM、schema 版本、availability 政策 ID。`dataset_id` = manifest 规范化 JSON 的 sha256，取代 Iceberg snapshot_id。

**available_time 政策**：官方归档无法证明历史行情的公开时刻，因此 `available_time` = bar 的 `close_time`（或成交的 `transact_time`）+ manifest 中声明的延迟 Δ。这是研究假设，必须绑定在 manifest 里并出现在每个结果中。

**读取契约**：
1. 唯一入口 `load(table, symbols, start, end, asof, columns)`；`asof` 必填，入口内强制 `available_time ≤ asof`。
2. 惰性扫描 + 列投影 + 谓词下推（`pl.scan_parquet` 或 DuckDB `read_parquet`，hive 分区裁剪）；返回 Arrow 列式内存，Polars / NumPy 零拷贝共享。
3. 读路径上不构造逐行 Pydantic 对象、不做 Python 逐行循环、不用 `Decimal`。原逐行模型承担的不变量（时间单调、主键唯一、覆盖区间、`available_time ≥ 事件时间`）改为写入时的向量化断言，失败则整文件拒绝。
4. 加载时校验 manifest：默认比对文件大小与行数，`--verify` 时比对 sha256。
5. 写入 = 临时文件 + 同文件系统原子 rename；每表单写者。

**新增依赖**：polars、duckdb、numpy（uv 锁定版本）。

## 4. 去状态爆炸规范

**适用范围**：作用于行情、特征、信号、仓位的代码。**不适用**：控制面状态机（生命周期、运行状态）、解析器的 fail-closed 分支、校验与实盘拒绝门。

| # | 规约 |
|---|---|
| S1 | 禁止对市场变量做 `if / elif / match` + 数值比较来决定信号或仓位；禁止按 Regime 标签查表配权；禁止用枚举状态做仓位开关。 |
| S2 | 状态一律表达为连续分数：滚动百分位秩（0~1）、`tanh(k·z)`、sigmoid、线性斜坡 + `clip`。仓位 = 连续因子的乘积。 |
| S3 | 平滑不等于无参数：每个尺度参数（k、窗口、斜坡宽度）来自 spec，代码里没有默认值，并计入试验次数（R4）。 |
| S4 | 全部向量化：Polars 表达式或 NumPy 数组运算；不逐行循环；复杂度 O(T) 或 O(T·W)。 |
| S5 | 路径依赖逻辑（滞回、回撤控制）是显式例外：在 spec 中声明，用单次 O(T) 递推核实现，不得每个时点重放历史。 |
| S6 | 每个特征必须通过**截断不变性测试**：在前缀数据上算出的值与在全量数据上算出的同一行的值按位相等（向量化下的防前视检查）。 |
| S7 | `research/sandbox/` 由一个 AST 检查测试执行 S1：比较表达式中出现数值字面量即失败（0 与符号判断除外）。 |

示例（把三档 Regime 开关改为连续缩放）：

```python
vol_rank = pl.col("rv").rolling_rank(window) / window  # 0~1
trend = (k_trend * pl.col("er_signed")).tanh()  # -1~1
position = trend * (1.0 - vol_rank).clip(0.0, 1.0) * target_scale
```

## 5. MVP 垂直切片：`research/sandbox/mvp_slice.py`

**输入**：manifest 路径；symbols（BTCUSDT、ETHUSDT）；`[start, end)`；`asof`；spec 文件（特征窗口、尺度参数、`fee_rate`、`slippage_rate`、决策延迟）。

**流程**：`load` → 特征（1m 对数收益、实现波动率、由 aggTrades 聚合到 1m 的主动买卖失衡）→ 连续信号 → 仓位 → 向量化回测（下一根开盘成交，计成本）→ 指标。

**输出**（`data/runs/<run_id>/`，不入 Git；失败的运行同样保留）：
- `result.json`：净收益、Sharpe、最大回撤、换手、成本占比、bar 数；复现元组（git commit、dataset_id、spec 哈希、依赖版本、availability 政策 ID）。
- `positions.parquet`：逐 bar 的信号、仓位、成交价、成本、净值。
- Pydantic 只在边界使用两次：读 spec、写 result。

**性能标准**（WSL、热缓存；首次实测后把实测值写回本节，修订须记录原因）：

| 操作 | 标准 |
|---|---|
| 加载 2 个 symbol × 1 年 1m K 线（约 105 万行，投影列） | ≤ 100 ms |
| 加载 1 个 symbol × 1 天 aggTrades | ≤ 300 ms |
| 1 年 1m 数据的完整切片（加载到指标） | ≤ 5 s |
| 峰值 RSS | ≤ 2 GiB |

**切片不是策略结论**：它验收的是管线，不产生任何可晋升的策略；切片的回测数字不得作为策略有效的证据。

## 6. Phase 1 敏捷垂直切片验收

1. 真实 Binance 归档的 BTC / ETH 1m K 线与 aggTrades 落盘到 §3 布局，manifest 与 CHECKSUM 一致。
2. `mvp_slice.py` 对真实数据端到端运行，同输入重跑按位一致。
3. S6 截断不变性测试与 S7 AST 检查通过；`asof` 之后的数据无法被读到（有测试）。
4. §5 性能标准有实测记录。
5. 保留代码的 pytest / ruff / ruff format / mypy 全绿。

## 7. 本文件取代的旧决定（ADR-0111）

- ADR-0021 对“Parquet + 自有快照清单”的否决，以及 ADR-0075 / 0108 的后续工作。
- roadmap Phase 1 验收矩阵 #1~#21 作为 Phase 1 关闭条件的地位（已有证据保留）。
- “全阶段框架代码先行”的开发顺序：Phase 2~14 标记为 FROZEN / ARCHIVED，切片验收前不重开。
