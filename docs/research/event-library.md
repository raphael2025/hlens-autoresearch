# Event Library

| 字段 | 值 |
|---|---|
| 类型 | `event` |
| 状态 | Phase 3 框架条目（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED） |
| 首次填充 | Phase 3（[ADR-0036](../adr/0036-event-provider-contract.md)） |

离散市场事件及其交互（EventSpec）。

## 条目字段（以 `core/contracts/event.py` 与 `core/domain/specs.py` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识（`EventSpec.ref`）；定义内容由 spec hash 绑定 |
| `trigger` | 触发条件：`{"operator": ..., <params>}` 的规范 JSON（`EventSpec` 没有 `params` 字段，参数全部在此） |
| `observable_time` | 事件被观测到的时间：所引用输入中最晚可见的时刻 + `observable_lag`（ADR-0036 §2，执行器与契约强制） |
| `interactions` | 与其他事件的时序 / 共现关系（交互算子的 `lineage` 与 `upstream_event_ids`） |
| `frequency` | 历史频率估计（`research/events/stats.py`；尚未在真实数据上运行） |
| `provider` | 实现插件（`plugins/events/`） |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。
- 事件定义不得使用未来确认（roadmap Phase 3 禁止事项）：执行器要求截至 t 的事件表不因之后的数据回填或撤回。
- 水平、窗口、倍数是**事件定义参数**，不是验证阈值；本库条目不携带任何验证阈值。

## 条目索引

以下是**事件算子模板**（operator）：具体条目 = 模板 + 具体 Feature / State 引用 + 参数，各自有 `name@version` 与 spec hash。
下方记录一个可复现的具体定义；它仍未进入 Control Plane Registry，也没有真实数据验证。

| 算子（operator） | 摘要 | 输入 | 可观测时间 | provider | 出处 | 状态 |
|---|---|---|---|---|---|---|
| `feature_threshold_cross` | Feature 相邻两点跨越固定水平 `level`（`up`：`prev <= level < value`；`down` 对称；`both`） | 1 个 Feature | 后一点可见 + lag | `feature_threshold_cross@1.0.0` | ADR-0036 | UNVERIFIED |
| `volatility_breakout` | （波动率）Feature 进入 `value > multiplier * 前 window 值均值` 区域（上一点不在区域内） | 1 个 Feature | 最新点可见 + lag | `volatility_breakout@1.0.0` | ADR-0036 | UNVERIFIED |
| `state_switch` | State 标签在相邻两个可计算点之间改变（可限定 `from_state` / `to_state`） | 1 个 State | 新状态点可见 + lag | `state_switch@1.0.0` | ADR-0036 | UNVERIFIED |
| `event_sequence` | A 之后 `window` 内的 B（交互：每个 B 链接之前最近的 A） | 2 个上游事件定义 | B 的事件时间 + lag | `event_sequence@1.0.0` | ADR-0036 | UNVERIFIED |
| `event_co_occurrence` | A、B 相距不超过 `window`（任一顺序） | 2 个上游事件定义 | 较晚者的事件时间 + lag | `event_co_occurrence@1.0.0` | ADR-0036 | UNVERIFIED |
| `event_window_end` | 每个上游事件 A 的窗口结束：事件时间 = A + `window`（`observable_lag` 恰为 `window`） | 1 个上游事件定义 | A 的事件时间 + `window` | `event_window_end@1.0.0` | ADR-0061 | UNVERIFIED |
| `event_absence` | 锚事件，且 `[锚 - window, 锚]`（闭区间）内无 `absent` 事件 | 2 个上游事件定义 | 锚的事件时间 + lag | `event_absence@1.0.0` | ADR-0061 | UNVERIFIED |
| `event_count` | 在事件 e 处，`[e - window, e]`（闭区间）内至少 `at_least` 个上游事件 | 1 个上游事件定义 | e 的事件时间 + lag | `event_count@1.0.0` | ADR-0061 | UNVERIFIED |

## 具体定义（尚未进入 Control Plane Registry）

以下定义仅组合仓库已有 Feature / State / Event Provider 的语义，不是 Knowledge Base 或 Control Plane Registry 条目；没有关联实验或真实数据证据。

| 字段 | 值 |
|---|---|
| `name@version` | `bar_log_return_zero_up_cross@1.0.0` |
| 状态 | `UNVERIFIED / NOT_VALIDATED`；未注册、未在真实数据上运行或校准 |
| 摘要 | `bar_log_return` 从非正值跨到正值时产生向上穿越事件 |
| 输入 / Provider | `feature:bar_log_return@1.0.0` / `feature_threshold_cross@1.0.0` |
| 触发定义 | `{"operator":"feature_threshold_cross","feature":"feature:bar_log_return@1.0.0","level":"0","direction":"up"}` |
| 来源 | 仓库实现：[ADR-0030](../adr/0030-feature-provider-contract.md)、[ADR-0036](../adr/0036-event-provider-contract.md)、`BarLogReturnProvider` 与 `FeatureThresholdCrossProvider`；未声称外部实证来源 |

实现按相邻且连续的 bar 计算对数收益；只有相邻可计算 Feature 点满足 `previous <= 0 < value` 时触发，缺值不填补且不跨过配对。事件可观测时间不早于输入 `available_time`。此草稿没有绑定具体 `FeatureSpec` 内容哈希，也不代表市场有效性或经济意义。

### `bar_realized_vol_5_20_squeeze_to_expansion@1.0.0`

| 字段 | 值 |
|---|---|
| `name@version` | `bar_realized_vol_5_20_squeeze_to_expansion@1.0.0` |
| 状态 | `UNVERIFIED / NOT_VALIDATED`；尚未进入 Control Plane Registry，未在真实数据上运行或校准 |
| 摘要 | 短窗相对长窗的实现波动率状态从 `squeeze` 切换到 `expansion` 时产生事件 |
| 输入 / Provider | `feature:bar_realized_vol_5@1.0.0`、`feature:bar_realized_vol_20@1.0.0` → `volatility_squeeze_bar5_bar20_test_fixture@1.0.0` → `state_switch@1.0.0` |
| 触发定义 | `{"operator":"state_switch","state":"state:volatility_squeeze_bar5_bar20_test_fixture@1.0.0","from_state":"squeeze","to_state":"expansion"}` |
| 来源 | 仓库实现：`BarRealizedVolatilityProvider`、`VolatilitySqueezeProvider`、`StateSwitchProvider`；阈值取自 `tests/plugins/states/test_state_volatility_events.py` 的测试夹具，仅用于定义可复现输入，不是推荐参数或实证来源 |

可复现的 Provider spec 构造为：`BarRealizedVolatilityProvider.spec(window=5, scale=18, available_lag=timedelta(0), bar_input=representation:canonical_bar_1m@1.0.0, version="1.0.0")` 与对应的 `window=20` spec；将两者的 `ref` 传给 `VolatilitySqueezeProvider.spec(short_vol_feature=..., long_vol_feature=..., squeeze_below="0.5", expansion_above="1.5", labels=("squeeze", "normal", "expansion"), name="volatility_squeeze_bar5_bar20_test_fixture", version="1.0.0")`；最后构造 `StateSwitchProvider.spec(state=..., from_state="squeeze", to_state="expansion", name="bar_realized_vol_5_20_squeeze_to_expansion", version="1.0.0", observable_lag=timedelta(0))`。这些显式参数用于稳定复现 spec 与 hash；`0.5` / `1.5` 来自测试夹具，未校准且不表达有效性主张。

状态切换只在相邻且连续的可计算状态点之间判断；缺值会打断配对，不跨越缺口。事件在新状态点可见时产生（此定义 `observable_lag=0`），不使用未来确认。该定义尚未绑定具体输入 spec 的内容哈希，也没有实验结果。

### 交互 DSL（ADR-0061）

交互表达式是**数据**（JSON 树，`plugins/events/dsl.py`），不是代码：叶子 `{"ref": "event:<name>@<semver>"}` 引用已登记的事件规格，
内部节点只有四个已审阅算子，参数全部显式、无默认，未知节点 / 算子 / 字段即拒绝；编译限额 `max_depth` / `max_nodes` 必填。
编译按后序生成上表中的普通交互规格（名称由内容派生，`dsl_<op>_<hash16>`），每一跳经执行器的上游核对。

| DSL 算子 | 语义 | 编译为 | 事件时间 |
|---|---|---|---|
| `seq(a, b, within_us)` | A 之后 `within` 内的 B | `event_sequence` | B 的事件时间 |
| `and(a, b, within_us)` | A、B 相距不超过 `within`（操作数按 spec hash 规范排序） | `event_co_occurrence` | 较晚者的事件时间 |
| `not(a, b, within_us)` | A 发生，且 `[A, A + within]` 内无 B | `event_window_end(a)` → `event_absence(·, b)`（两跳） | 窗口结束 A + `within` |
| `count(a, at_least, within_us)` | 在 A 处，`[A - within, A]` 内至少 `at_least` 个 A | `event_count` | 该 A 的事件时间 |

同一表达式（同一 registry）永远编译成同一组规格与哈希；`Compilation.record()` 记录表达式哈希与全部规格哈希，
`verify_compilation` 可重算核对。试验账本（`research/hypotheses/ledger.py`）记录 Hypothesis 登记（含失败）与已登记 Hypothesis 的预登记重评；DSL 编译本身不产生账本条目。参数点批次会将每格转换成独立 Hypothesis 并在运行前预登记。当前尚未定义 P7 各组合算子到 Hypothesis / TrialLedger 的完整计数归属，因此不宣称底层每个事件表达式组合都各自形成一笔账本试验。

## 规格状态与缺口

规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）：`IMPLEMENTED` = 有代码与定向测试（FRAMEWORK_IMPLEMENTED /
CODE_COMPLETE，DEBUG_PENDING；不是验收）；`NOT_VALIDATED` = 没有在正式 Research Dataset 上运行并经验证的结果；`UNSPECIFIED` = 未被已接受
ADR / 契约定义。上表 8 个算子与 4 个 DSL 算子均为 `IMPLEMENTED · NOT_VALIDATED`；本文件记录一个具体事件定义，但它未进入 Control Plane Registry，亦未验证。

测试证据：`tests/plugins/events/test_event_providers.py`、`test_window_providers.py`、`test_dsl.py`；`tests/contract_suites/event.py`（因果扰动、
PIT 一致）；`tests/infrastructure/event/`（执行器、未来确认拒绝、上游核对、DSL 编译）；`tests/research/events/test_event_stats.py`；
`tests/smoke/test_phase3_events_smoke.py`（合成数据）。

外部知识库（`hlens-knowledge`，只读参考，不是 `KnowledgeItem`、未接入本项目）中与事件相关的条目及映射：

| 知识库条目 | 内容 | 映射 | 规格状态 |
|---|---|---|---|
| `MST-SHOCK-001` | 冲击：`\|r_t\| > k·σ` 或跳跃检验，须预注册 | 可由 `feature_threshold_cross` / `volatility_breakout` 在已实现特征上近似，但"`k·σ` 的动态水平"不是现有算子（`feature_threshold_cross` 的水平是常数） | `UNSPECIFIED` |
| `MST-SQUEEZE-001` → `MST-EXPANSION-001` | 压缩后扩张 | `state_switch`（`from_state=squeeze` / `to_state=expansion`）作用于 `volatility_squeeze_bar5_bar20_test_fixture`；见上方 `bar_realized_vol_5_20_squeeze_to_expansion@1.0.0` | `UNVERIFIED · NOT_VALIDATED`（尚未进入 Registry） |
| `RM-EVENT-STUDY-001` | 事件研究：异常收益与 CAR（MacKinlay 1997） | `research/events/stats.py` 只做频率、共现、领先滞后、重叠描述，不计算异常收益；标签见 [outcome-library.md](outcome-library.md) | `UNSPECIFIED` |

已知失败模式（roadmap）：事件重叠导致样本非独立（`overlap_diagnostics` 描述）、组合爆炸（按 04-research-loop.md §4，获准进入研究的组合需在执行前纳入计数；当前 P7 执行尚未批准，DSL 编译不计数）。底层组合与假设登记之间的计数映射仍是 P7 设计项；不得把事件 DSL 编译描述成已登记试验。事件频率过低由 `event_frequency` 描述。
