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

## Implementation note (2026-09-26)

状态 **CODE_COMPLETE / DEBUG_PENDING**。无 core / 契约 / Schema 变化，无新数值阈值，缺证据从不算 PASS。

- **注册表与门**（`research/validation/benchmark.py`）：`MARKET_BENCHMARK_RULES` = `none` / `buy_and_hold_equal_weight` / `flat`，按名称**精确**匹配
  （不做大小写 / 空白归一）。未登记名称 → `G2.market_benchmark` = `INCONCLUSIVE`（`configuration_missing:benchmark.market_benchmark_rule=<名>`）。
  计算出的报告项：`G2.market_benchmark.<规则>`（策略复利净收益 − 基准复利净收益）、`.benchmark_net_return`、`.period_excess_mean`、
  `.period_excess_positive_fraction`（逐期净超额序列的摘要），以及 `G2.inverse_control`（取反重跑的复利净收益）。报告项无阈值、判 `PASS` =
  已计算（与 `G2.cost_report.<i>` 相同），因此不改变 `derive_verdict`。证据无法产生（执行模型无法复现、重跑出错、逐期网格不一致）→ 该项
  `INCONCLUSIVE`（`benchmark_unavailable:<原因>`）。`none` 不产生门；`flat` 在本模块内构造零序列，不重跑。
- **流水线**（`pipeline.py`）：`InSampleInput.benchmark`（证据源，默认 `None`）；给出时这些项位于 G2 末尾、G3 之前，证据源只在到达 G2 且确有需要
  计算的项时被调用一次。`None` 不加任何门——纯标签流水线没有价格路径，无法计算市场基准。
- **回测接线**（`research/strategies/validation.py`）：`ValidatorSetup.market_benchmark`（默认 `False`）。`True` 时先用声明的回测器（未声明则
  `BarBacktester()`）重跑所选试验自身的目标并要求 `result_hash` 完全一致，然后以同一回测器、成本模型、bar、初始权益回测：等权买入持有
  （在重跑的首个决策时刻按 `1 / N` 建仓、持有到数据末尾；目标的唯一"输入"是规则本身，`latest_input_available_time` = 决策时刻）与每个目标取反
  （空仓保持空仓）的反向对照。它们是回测而非 `TrialRunner` 调用：试验数与 `TrialRunner` 调用序列不变（测试固定）。
- **多标的**（`instruments.py`）：只在池化输入上计算，等权覆盖全部已验证标的；逐标的输入带证据源会被拒绝（`ValueError`），不产生逐标的副本。
- **逐字节不变**：`market_benchmark=False`（研究循环、合成实验室、e2e 等全部既有调用方）时报告与视图不变；b3986da 上固定的单标的 / 多标的报告与
  视图哈希由 `tests/research/strategies/test_market_benchmark.py` 复现。**没有任何既有固定哈希改变**，也没有修改任何 TEST ONLY 夹具。
- **已知限制 / 后续（DEBUG_PENDING）**：采用调用方显式开启而不是按 Profile 自动启用，原因是现有 TEST ONLY Profile（研究循环、合成实验室、e2e 夹具）
  都用未登记的占位名 `"test-only"`：自动启用会把这些报告全部变为 `INCONCLUSIVE`，而这些夹具与 `research/loop` 不在本批次的文件边界内。
  研究循环接入（`research/loop` 设置 `market_benchmark=True`）并把这些夹具改为已登记的规则名、重新固定其记录哈希，需另行批准；在此之前，
  未开启的调用方不会因未登记规则名而得到 `INCONCLUSIVE`。`docs/architecture/07-validation.md` §G2 的门清单尚未同步（不在本批次边界内）。
