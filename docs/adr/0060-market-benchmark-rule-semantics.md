# ADR-0060：C-T4 市场基准规则（`benchmark.market_benchmark_rule`）与反向对照的语义

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 授权（通过 ADR 自主决定技术方案与红线事项） |
| 相关 Phase | Phase 4 / 5（C-T4）、Phase 8 |
| 影响范围 | `research/validation/`（新增基准规则解析与报告门）、`research/strategies/validation.py`（输入构建）；**无契约 / Schema 变化**（字段已存在） |
| 是否破坏兼容 | 否：Profile 字段不变；新增的只是报告项 |

## 背景

宪法 C-T4："必须与空模型（相同交易次数与持仓分布下的随机进出）比较；市场基准按策略类别适用。具体设定由 Profile 定义。"
`ValidationProfile.benchmark` 已有 `null_model*`（G2 `null_model_percentile` 门已实现）与 `market_benchmark_rule: str`、`inverse_control_reported: bool`，
但后两者**没有任何代码读取**（全代码复核审计），也没有定义字符串的含义。

## 裁决

1. `market_benchmark_rule` 取值是一个**已登记的规则名**（与 `capacity.impact_model` 同一模式；未知名称 → 该项 `configuration_missing` INCONCLUSIVE，从不 PASS）。首批规则：
   - `none`：该策略类别不适用市场基准——报告"不适用"，不产生门；
   - `buy_and_hold_equal_weight`：在与策略相同的研究窗口、相同标的集合、相同成本模型下的等权买入持有；报告策略相对它的超额收益与各期超额序列；
   - `flat`：零敞口基准（等价于"是否跑赢现金"），报告同上。
2. 市场基准与反向对照都是**报告项**（`G2.market_benchmark.<rule>`、`G2.inverse_control`，`reported_only`，判定为 `PASS` 仅表示"已计算"，不参与整体判定）：
   C-T4 的门槛是空模型（已实施）；"按策略类别适用"的市场基准是证据而非否决条件——否则要为每个类别发明一个超额阈值，违反"阈值只来自 Profile"。
3. `inverse_control_reported = true` 时，把同一策略的每个目标仓位取反（同一执行与成本模型）重跑，报告其净收益；为 `false` 时不计算。
4. 基准与反向对照都在同一次试验的稳健性重跑中计算，**不增加 trial 数**。

## 后果

- 正面：C-T4 的两个既有 Profile 字段第一次有确定的语义与实现；报告可见"是否只是跑赢了市场 / 反向是否同样赚钱"。
- 负面：市场基准不作否决条件——若以后要把某类别的超额设为门槛，须另立 ADR 并由 Profile 提供数值。

## 合规检查

- [x] 不写入任何数值阈值；不修改 Constitution 或 Profile 结构
- [x] 未知规则名从不 PASS；缺证据 INCONCLUSIVE
