# ADR-0065: G4 容量检查遇到结转未成交余量时失败关闭（数据集路径）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-27，Codex 依 Raphael 授权决定 B67 方案 A） |
| 日期 | 2026-09-27 |
| 决策者 | Codex（技术协调者，CLAUDE.md §0） |
| 起草者 | Claude Code（Opus），只记录决定，不改变决定 |
| 相关 Phase | Phase 4 / 8（C-R5 容量，G4）；ADR-0054（部分成交结转）、ADR-0041、ADR-0064 |
| 影响范围 | `research/strategies/validation.py` 的容量输入构造、`research/validation/g4.py::RobustnessInput`、`research/validation/robustness.py::capacity_check` 的一个新 INCONCLUSIVE 原因；不改 Contract / Schema / Constitution / Profile / 阈值 |
| 是否破坏兼容 | 否（无余量、零余量、合成路径与默认 `next_bar_open` 结果与哈希不变） |

## 背景（Context）

ADR-0054 的 `next_bar_open_participation`（结转）执行模型下，一个目标可以在回测结束时仍有未成交数量：`BacktestResult.remainders` 中
`remaining_quantity > 0`（`ended_by` 为 `superseded` 或 `end_of_data`）。G4 容量检查（C-R5，`capacity_check`）只看到**已执行**的成交，
因此恰在参与上限生效、需求未被满足时，可能高估容量。ADR-0054 实施说明把"G4 容量检查未改读结转结果"列为未做项。

## 决策（Decision）

1. **范围**：只针对 G4 的**数据集回测路径**（`ValidatorSetup.dataset_bars` 给出）。合成 / 无数据集路径不读取余量，逐字节不变。
2. **正余量 → 失败关闭**：所选试验重跑的回测中存在任一 `remaining_quantity > 0` 的结转余量时，`G4.capacity.estimated` = INCONCLUSIVE，
   稳定原因名 **`carry_over_unfilled`**；不从部分证据计算容量或冲击（不产生 `G4.capacity.required` / `G4.capacity.impact_estimated`）。
3. **details（稳定、可复核）**：`details["carry_over_unfilled"]` 记录正余量条数 `remainders` 与首个正余量（按契约的 `(decision_time, instrument)` 升序）的身份与字段：`instrument`、`decision_time`、`requested_quantity`、
   `filled_quantity`、`remaining_quantity`、`ended_by`、`ended_at`（时刻为 ISO 文本，数量为 `Decimal` 规范文本）。门的 value = 正余量条数。**不**记录跨标的的余量合计（不同标的的数量不可相加，Codex 复核决定）；该标的自身的余量在 `first.remaining_quantity` 中。
4. **不变**：没有余量（默认 `next_bar_open`，`remainders = ()`）或只有零余量（`ended_by = filled`）时结果与哈希完全不变。
5. **与既有原因的先后**（同一 `G4.capacity.estimated` 门只报告一个原因）：`profile_field_missing`（参与上限缺失）→ `bar_volume_source_mismatch`
   （ADR-0064）→ **`carry_over_unfilled`** → `bar_volume_missing` → `no_trades` → 计算。冲突证据优先于未满足需求，未满足需求优先于缺证据；
   全部未成交（没有成交）时报告 `carry_over_unfilled` 而不是 `no_trades`。
6. 不新增阈值；不改 Profile、Constitution、契约或 Schema；只增加研究内部类型与字段（默认值保持既有调用方不变）。本 ADR 与其实施**不构成**
   Phase 4 / 5 验收。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 正余量即 INCONCLUSIVE（选定） | 不从部分证据估计；与 ADR-0041 / 0064 的"不静默择一"一致 | 结转运行在参与上限生效时 G4 容量不可判 | Codex 选定 |
| B. 只在 details 报告余量，仍按成交估计 | 仍有估计值 | 可能高估容量 | 未选 |
| C. 维持现状 | 无改动 | 静默高估 | 未选 |

## 后果（Consequences）

- 正面：结转模型下的容量估计不会基于被截断的需求。
- 负面：参与上限经常生效的策略在数据集路径上 G4 容量为 INCONCLUSIVE，直到其执行不再留下余量。

## 合规检查

- [x] 不修改 Domain Contract、Schema、Constitution、Validation Profile；不新增数值阈值
- [x] 余量存在时从不 PASS；缺证据仍为 INCONCLUSIVE
- [x] 合成路径、默认 `next_bar_open`、无 / 零余量的结果与哈希不变

## Implementation note（B67，2026-09-27）

状态 **CODE_COMPLETE / DEBUG_PENDING**。无 core / 契约 / Schema / Profile / 阈值变化；不构成 Phase 4 / 5 验收。

- `research/validation/robustness.py`：常量 `CARRY_OVER_UNFILLED`；`capacity_check(..., remainders=None)`（默认 `None` = 不读取，既有调用方不变）；
  在 `bar_volume_source_mismatch` 之后、`bar_volume_missing` 之前新增分支：任一 `remaining_quantity > 0` → `G4.capacity.estimated` INCONCLUSIVE
  `carry_over_unfilled`，value = 正余量条数，不计算容量 / 冲击；`details["carry_over_unfilled"]` = `{"remainders": 条数, "first": {instrument, decision_time,
  requested_quantity, filled_quantity, remaining_quantity, ended_by, ended_at}}`（首个按契约 `(decision_time, instrument)` 升序的正余量；时刻 ISO 文本、
  数量 `Decimal` 规范文本）。按 Codex 复核**不**记录跨标的余量合计（不同标的的数量不可相加）。
- `research/validation/g4.py`：`RobustnessInput.capacity_remainders`（默认 `None`），传给 `capacity_check`。
- `research/strategies/validation.py`：`robustness_input` 只在数据集路径（`dataset_bars` 给出）传入所选重跑回测的 `remainders`；合成路径为 `None`。
- 测试：`tests/research/validation/test_robustness.py`（无 / 空 / 零余量与基线载荷哈希相同；正余量 INCONCLUSIVE、details 精确、无容量 / 冲击；
  与 `profile_field_missing` / `bar_volume_source_mismatch` / `bar_volume_missing` / `no_trades` 的先后）；`tests/research/strategies/test_backtest_validation.py`
  （数据集路径结转运行留下正余量 → INCONCLUSIVE、details 与回测余量一致、无合计键；同一结转运行在合成路径不读取；默认模型无余量、容量与合成路径相同、
  `capacity_remainders` 数据集路径为 `()`、合成路径为 `None`；数据集路径与合成路径完整评估的报告内容哈希等于 B67 之前 `255ce1a` 源码上算得的权威基线）。
- 原始运行记录（含失败，按时间顺序）：
（全部 6 GB 上限、未过滤管道或 `set -o pipefail`、记录 pytest 自身退出码）
1. 第一次定向运行（`pytest tests/research/validation/test_robustness.py tests/research/strategies/test_backtest_validation.py -k "remainder or carry or unchanged or reasons or volume or capacity"`）→ `1 failed, 14 passed, 56 deselected in 120.60s`，**退出码 1**：失败的是 `test_a_positive_remainder_is_inconclusive_and_computes_nothing`，原因是测试夹具 `_remainder(..., requested="-4")` 把 `filled_quantity` 算成 `requested − remaining`，违反契约的有符号数量不变量（`FillRemainder` 以 `成交数量绝对值之和超过目标变化量` 拒绝构造）；**被测规则本身不是失败原因**。
2. 夹具修正（`filled = requested − sign(requested) × remaining`）后同一命令 → `15 passed, 56 deselected in 119.75s`，退出码 0。
3. 反向核对（B67 之前的源码）：`test_robustness.py` 因新常量不存在而收集失败（1 error）；`test_backtest_validation.py -k "remainder or carry"` → `2 failed, 3 passed`（依赖本改动的两项失败，"只在数据集路径读取"不变量两边都通过）。
4. 一次 B67 回归运行在代码随复核修改后被主动停止（被取代，退出码 144，无结果，不计）。
5. Codex 复核修改：移除跨标的 `remaining_quantity_total`；正余量用例改用合法正请求量；补充权威基线。基线第一次计算因工作树守卫拒绝了合并命令、实际在 B67 源码上运行，**作废**；随后逐条命令把三个源文件切回 `255ce1a` 版本（核对 B67 符号计数为 0、与 HEAD 无差异），两次计算结果一致：数据集路径报告 `8ed6bf10ce3a1a2b1cab21d383468f92aece3603c74fcfb23006739d566b3a6a`、合成路径报告 `f46de6b1b9c047e5743ff9d5676f3e640aea7f2f47b05f00092d7e43acdbbd76`、容量检查载荷 `847eefcf40a70729c97c2dcf2bc21bf5d91e29b6cefcdddc089035360f14bf87`，已钉入测试。
6. 修改后同一定向命令 → `15 passed, 57 deselected in 121.27s`，退出码 0；三项基线 / 正余量用例单独运行 → `3 passed in 9.87s`，退出码 0。
7. 最终代码的完整 B67 回归（`pytest -q -rs -m "not postgres" tests/research/strategies tests/research/validation tests/research/synthetic_lab tests/research/loop` + 两个非 PostgreSQL 数据集循环 e2e + 文档一致性 + 架构边界）→ `1 failed, 719 passed, 1 warning in 1462.54s`，**退出码 1**：唯一失败为 `tests/test_docs_consistency.py::test_adr_index_matches_adr_status`（ADR-0065 尚未登记到 ADR 索引；代码与其他测试全部通过）。
8. 补 ADR 索引行后 `pytest tests/test_docs_consistency.py tests/test_architecture_boundaries.py` → `20 passed in 0.46s`，退出码 0（其余 719 项的输入未变，未整体重跑；最终 HEAD 的全量门禁会覆盖全部）。
- 未运行：PostgreSQL 标记测试；无网络、无安装。
