# ADR-0085: 研究库扩展批次（非契约）：候选特征、状态、策略与风控政策

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权（CLAUDE.md §0） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 1（Feature）、Phase 2（State）、Phase 5（Strategy / Risk） |
| 影响范围 | Plugin / Research；**不改** `core/` 契约、Constitution、Validation Profile 或阈值 |
| 是否破坏兼容 | 否：只新增 Provider / 策略 / 政策，既有对象的身份与哈希不变 |

## 背景

`feature-library.md`、`state-library.md`、`strategy-library.md`、`risk-library.md` 里有一批条目被标为 `DOCUMENTED · UNSPECIFIED`。它们的输入在 ADR-0022 的数据范围内都拿得到（bar OHLCV 与 K 线 taker 字段），公式在文献中也有公认定义，只是窗口、边界、缺值处理没有写成规格。2026-09-28 的模块补全审计（MOD-FEAT / MOD-STATE / MOD-VALID）把它们列为待决定。Raphael 要求开发阶段把底层代码补齐。

## 决策

### 通用规则（所有新增对象）

1. 每个新增对象都有新的 `name@1.0.0` 身份，现有对象一律不改。
2. **没有任何默认数值。** 所有窗口、阈值、周期、乘数都通过 spec 参数显式传入，缺一个就拒绝构造。这些数值属于实验预注册的一部分（Constitution C-T1 / C-T2），不能在库里预设。
3. 只向后看：每个时点只使用 `available_time ≤ t` 的输入（沿用 ADR-0030 / ADR-0035 的执行器截断）。历史不足时，按所在 Provider 族的现有约定返回缺失，不能外推或补值。
4. 数值计算沿用现有 bar 特征的精确 Decimal 风格。遇到除以零（例如零成交量、零区间）时，按缺失处理，不抛出也不返回任意值。
5. 每个对象都要有契约套件用例、手算精确值测试，以及未来数据扰动不改变过去结果的测试。
6. 实现后，把研究库中对应条目的状态更新为 `IMPLEMENTED · NOT_VALIDATED`。

### 特征（`plugins/features/`）

| 条目 | 定义（参数全部显式） |
|---|---|
| `FEA-PARKINSON-001` | Parkinson 区间方差 `(ln(H/L))² / (4 ln 2)` 的尾随 `window` 均值，再开方 |
| `FEA-GK-001` | Garman–Klass：`0.5 (ln(H/L))² − (2 ln 2 − 1)(ln(C/O))²` 的尾随均值，再开方 |
| `FEA-YZ-001` | Yang–Zhang：隔 bar 方差 + k × 开盘到收盘方差 + (1 − k) × Rogers–Satchell 方差，`k = 0.34 / (1.34 + (n+1)/(n−1))`，n = `window` |
| `FEA-JUMP-QV-001` | 已实现方差减去双幂变差 `(π/2) Σ |r_t||r_{t−1}|`，截断为 ≥ 0 |
| `IND-ATR-001` | Wilder ATR：真实波幅的 Wilder 平滑，`period` 显式 |
| `IND-RSI-001` | Wilder RSI：平均涨幅 / 平均跌幅的 Wilder 平滑；平均跌幅为 0 时 RSI = 100 |
| `IND-MACD-001` | EMA(`fast`) − EMA(`slow`)，信号线 EMA(`signal`)。输出 MACD 与信号线两个特征；EMA 用前 `slow` 个 bar 的 SMA 做种子 |
| `IND-BBANDS-001` | 布林带 %b 与带宽：SMA(`window`) ± `k` × 总体标准差 |
| `IND-VWAP-001` | 尾随 `window` 个 bar 的 `Σ(典型价 × 成交量) / Σ 成交量`，典型价 = (H+L+C)/3 |
| `FEA-TREND-STRENGTH-001` | Wilder ADX，`period` 显式 |
| `FEA-TAKER-FLOW-001` | K 线 `taker_buy_base_volume` 占比，按尾随 `window` 的成交量加权：`2 × Σ taker / Σ volume − 1` |
| `MSTX-AMIHUD-001` | 尾随 `window` 个 `|r| / quote_volume` 的均值 |
| `FEA-CS-SPREAD-001` | Corwin–Schultz 两 bar 高低价价差估计，负值截断为 0，再取尾随 `window` 均值 |

以下条目不在本批次：

- aggTrades 类特征（`ORF-*`），因为没有从成交到 Feature 的输入路径；
- `FEA-AR-SPREAD-001`，留待下批。

### 状态（`plugins/states/`）

| 条目 | 定义 |
|---|---|
| `MST-SQUEEZE-001` / `MST-EXPANSION-001`，实现为 `volatility_squeeze@1.0.0` | 比值 `bar_realized_vol_<short>` / `bar_realized_vol_<long>`，与显式的 `squeeze_below`、`expansion_above` 比较，输出 `squeeze` / `normal` / `expansion`；要求 `squeeze_below < expansion_above` |
| `MST-SHOCK-001`，实现为 `return_shock@1.0.0` | 当期 `|bar_log_return| > k × bar_realized_vol_<window>` 时为 `shock`，否则为 `calm`；`k` 与 `window` 显式 |

HMM / 转换期（`MST-TRANSITION-001`、`SYN-REGIME-001`）属于需要新模型族规格的训练型状态；risk-on/off 缺跨资产数据。两者都不在本批次。

### 策略（`research/strategies/`）

所有策略都是现货 **long / flat**：做空成本（缺口 ST-4）还没有定义，因此不产生空头目标。每个策略都按 ADR-0038 声明参数空间，Provider 拒绝空间外的参数点。

| 条目 | 定义 |
|---|---|
| `STR-TF-DONCHIAN-001`，实现为 `donchian_breakout@1.0.0` | 收盘价突破前 `entry_window` 根 bar 的最高价时持有，跌破前 `exit_window` 根 bar 的最低价时空仓 |
| `STR-MR-ZSCORE-001`，实现为 `zscore_reversion@1.0.0` | 收盘价相对 SMA(`window`) 的 z 值低于 `−entry_z` 时持有，回升到 `−exit_z` 以上时空仓；要求 `exit_z < entry_z` |
| `STR-DUAL-MOM-001`，实现为 `dual_momentum@1.0.0` | 在 BTCUSDT / ETHUSDT 中选 `lookback` 收益最高的标的；它的绝对动量 ≤ 0 时全部空仓。走 ADR-0059 的横截面声明 |

配对交易（需要空头腿）和横截面反转（缺宽标的池）不在本批次。

### 风控政策

| 条目 | 定义 |
|---|---|
| `RSK-DD-CONTROL-001`，实现为 `drawdown_control@1.0.0` | 权益从峰值回撤超过 `max_drawdown` 时，把目标仓位缩到 `reduced_fraction` 倍。`PortfolioState.equity` 缺失时 fail closed（ADR-0038 对路径依赖规则的要求） |

止损需要入场价路径，风控请求里没有这项输入；Kelly 需要边际估计；ES / CAViaR 是度量而不是规则。这些都不在本批次。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 为每个条目预设常用默认值（例如 RSI 14、布林 20 / 2） | 违反 C-T1 / C-T2：数值必须预注册，库里不能预设 |
| 等 Phase 4 校准后再实现 | 这些是 Provider 代码，不是验证数值；推迟只会拖延底层代码补全 |

## 后果

- 正面：研究库里可计算的候选全部有了实现，研究循环能组合的构件更多。
- 代价：新 Provider 会增加 Registry 条目和测试面，而且都未经调试。任何策略仍须通过 G0–G5 与 Promotion 才能晋升，本 ADR 不改变晋升条件。
