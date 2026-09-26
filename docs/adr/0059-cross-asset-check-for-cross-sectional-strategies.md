# ADR-0059：G4 跨资产检查（C-R3）对横截面策略的适用方式

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | 待 Raphael / Codex 批准（验证规则变化，H2 / H3） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 8 Validation & Robustness；Phase 5 横截面动量 `xsmom_bars` |
| 影响范围 | `research/validation/robustness.py`（`cross_asset_check`）、`research/strategies/validation.py`（G4 输入构建）；无契约 / Schema 变化 |
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
- [ ] 验证规则变化须经批准（H2 / H3）——待定；批准前不改代码
