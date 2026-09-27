# ADR-0059：G4 跨资产检查（C-R3）对横截面策略的适用方式

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26）；实施状态 CODE_COMPLETE / DEBUG_PENDING |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 授权（通过 ADR 自主决定，含红线） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 8 Validation & Robustness；Phase 5 横截面动量 `xsmom_bars` |
| 影响范围 | `research/validation/robustness.py`（`cross_asset_check`）、`research/validation/g4.py`（`RobustnessInput` 可选字段）、`research/strategies/validation.py`（G4 输入构建）、`research/strategies/cross_section.py`（横截面声明）；无 core / 契约 / Schema 变化 |
| 是否破坏兼容 | 否：单标的与时间序列策略的判定逐位不变 |

## 背景

`cross_asset_check`（C-R3）把回测按标的拆开，逐标的**单独重跑**，再要求正收益标的比例不低于 `cross_asset.min_positive_fraction`。横截面策略
（如 B22 的 `xsmom_bars`：在多个标的之间排序、多前空后）在只有一个标的的宇宙里按定义恒为空仓：每个单标的重跑的净收益都是 0，正收益比例恒为 0。
因此只要给出阈值，G4 跨资产检查对**任何**横截面策略都结构性 FAIL，与策略好坏无关（`tests/research/strategies/test_cross_sectional_momentum.py`
已固定这一现状，未改任何门）。这不是保守，而是错误的证据：FAIL 会进入 Failure Registry，把"检查不适用"记成"策略被否定"。

## 决策包

- **问题**：C-R3 对"必须在多个标的之间才有定义"的策略如何检验？
- **选项**：
  - A. **子宇宙检验**：对横截面策略，把声明的标的集划成若干不相交子宇宙（每个 ≥ 2 个标的；划分规则确定、写入报告），逐子宇宙重跑并按同一 `min_positive_fraction` 判定；
    标的数不足以划出 ≥ 2 个子宇宙时判 `INCONCLUSIVE`（`not_enough_instruments_for_subuniverses`）。
  - B. **留一法**：逐次去掉一个标的在其余标的上重跑，要求正收益比例达标。
  - C. **过渡规则**：单标的重跑全部零敞口时，该门判 `INCONCLUSIVE`（`not_applicable_zero_exposure_single_asset`），不计算比例；不改其他情形。
- **推荐**：先实施 **C**（立即消除结构性误判，且只会把 FAIL 变为 INCONCLUSIVE——仍不能晋升，不会多放行），再以 **A** 作为横截面策略的正式 C-R3 检验
  （B 的各次重跑高度重叠，独立性差）。策略是否为横截面由其 `StrategySpec` / provider 的声明决定，不由结果推断。
- **不决定时保持不变**：横截面策略在给出 `min_positive_fraction` 时 G4 跨资产恒 FAIL；它们实际上不可能通过验证。

## 合规检查

- [x] 不写入任何数值阈值；沿用既有 `cross_asset.min_positive_fraction` 来源规则（ADR-0041 / ADR-0052）
- [x] 动机不是让某个实验通过：C 只把不适用的情形从 FAIL 改为 INCONCLUSIVE，仍阻止晋升；A 是更强的检验
- [x] 验证规则变化须经批准（H2 / H3）——Claude Code（Opus）依 Raphael 2026-09-26 授权（通过 ADR 自主决定，含红线）批准：同时实施 C 与 A（2026-09-26）

## 决定（2026-09-26）

同时实施 **C**（过渡安全规则）与 **A**（横截面策略的正式 C-R3 检验）；不实施 B。

## Implementation note (2026-09-26)

状态 **CODE_COMPLETE / DEBUG_PENDING**。无 core / 契约 / Schema 变化，无新数值阈值，缺失从不算 PASS。

- **C（零敞口判据）**：`research/strategies/validation.py` 为每个声明标的的单标的重跑记录 `per_asset_exposed[<名>]`：
  重跑中**至少一个非零的执行目标**（风控 / 延迟之后、回测实际执行的 `TargetPosition`）**或至少一笔成交**即为有敞口。
  判据只看持仓，不看收益：有持仓但净收益为 0 照常计算比例。只有当**每个**声明标的都已知无敞口时（映射缺失或缺某个标的 = 未知，
  不视为零），`cross_asset_check` 的 `G4.cross_asset.positive_fraction` 判 `INCONCLUSIVE`（metric
  `not_applicable_zero_exposure_single_asset`，值 = 标的数），不计算比例，`details.zero_exposure` 记录判据与标的。
  缺阈值（`profile_field_missing`）与未测标的（`declared_instruments_not_tested`）仍优先。阈值为 0 时零敞口也不会 PASS。
- **A（声明）**：`research/strategies/cross_section.py` 的静态集合 `CROSS_SECTIONAL_STRATEGIES`（按 `StrategySpec.name`；首个为
  `xsmom_bars`），`is_cross_sectional(spec)` 只读规格名，从不由结果推断（同一规则换名即不是声明的横截面策略，走 C）。
  规则改变不再是横截面时须改名。
- **A（划分）**：`research/validation/robustness.py` 的 `subuniverse_partition`，规则 `SUBUNIVERSE_RULE` =
  `sorted_unique_consecutive_pairs_odd_remainder_joins_last`：去重、排序、按相邻两两切分，奇数个时最后一个并入最后一组；
  每组 ≥ 2（`MIN_CROSS_SECTION`，截面的结构定义，不是校准数值）。可划出的子宇宙少于 2 个（少于 4 个标的）→ `()`。
- **A（重跑与判定）**：对声明的横截面策略，G4 输入构建在逐标的重跑之后，对每个子宇宙调用一次
  `TrialRunner.run(params, instruments=<子宇宙>)`，得到 `RobustnessInput.sub_universes`（`SubUniverse`：标的、逐期收益、是否有敞口）。
  `cross_asset_check` 校验子宇宙恰为 `subuniverse_partition(declared)`（否则 `ValueError`），以**同一** `cross_asset.min_positive_fraction`
  （同一来源规则）判定净收益为正的子宇宙比例（metric `positive_subuniverse_fraction`）；子宇宙不足 → `INCONCLUSIVE`
  `not_enough_instruments_for_subuniverses`。逐标的行照常写入 `details`，但不对横截面策略判门；`details.cross_section` 记录规则与每个子宇宙。
- **试验数不变**：子宇宙重跑与逐标的重跑一样是所选试验的稳健性重跑，`family_trial_count` 与 `trials` 不变（测试固定）。
- **逐字节不变**：未声明横截面的策略（单标的、时间序列）的 `TrialRunner` 调用序列不变；其各自重跑有敞口时 `cross_asset_check`
  输出不变。改动前（4543036）固定的报告 / 视图 / G4 哈希由 `tests/research/strategies/test_cross_sectional_g4.py` 复现。
- **已知限制（DEBUG_PENDING）**：子宇宙划分只按标的名，不考虑流动性 / 板块；横截面声明是研究层的静态集合，尚无契约层表达；
  4 个标的只得到 2 个子宇宙，此时 `min_positive_fraction` 的分辨率很粗（阈值仍未校准，属 Phase 9）。
