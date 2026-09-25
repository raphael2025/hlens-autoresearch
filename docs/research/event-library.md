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
目前只在合成夹具与冒烟测试上实例化，尚无登记的具体条目。

| 算子（operator） | 摘要 | 输入 | 可观测时间 | provider | 出处 | 状态 |
|---|---|---|---|---|---|---|
| `feature_threshold_cross` | Feature 相邻两点跨越固定水平 `level`（`up`：`prev <= level < value`；`down` 对称；`both`） | 1 个 Feature | 后一点可见 + lag | `feature_threshold_cross@1.0.0` | ADR-0036 | UNVERIFIED |
| `volatility_breakout` | （波动率）Feature 进入 `value > multiplier * 前 window 值均值` 区域（上一点不在区域内） | 1 个 Feature | 最新点可见 + lag | `volatility_breakout@1.0.0` | ADR-0036 | UNVERIFIED |
| `state_switch` | State 标签在相邻两个可计算点之间改变（可限定 `from_state` / `to_state`） | 1 个 State | 新状态点可见 + lag | `state_switch@1.0.0` | ADR-0036 | UNVERIFIED |
| `event_sequence` | A 之后 `window` 内的 B（交互：每个 B 链接之前最近的 A） | 2 个上游事件定义 | B 的事件时间 + lag | `event_sequence@1.0.0` | ADR-0036 | UNVERIFIED |
| `event_co_occurrence` | A、B 相距不超过 `window`（任一顺序） | 2 个上游事件定义 | 较晚者的事件时间 + lag | `event_co_occurrence@1.0.0` | ADR-0036 | UNVERIFIED |

已知失败模式（roadmap）：事件重叠导致样本非独立（`overlap_diagnostics` 描述）、组合爆炸（每个交互组合计入 trial count，
04-research-loop.md §4）、事件频率过低（`event_frequency` 描述）。
