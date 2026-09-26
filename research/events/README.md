# research/events

Phase 3 — Event & Interaction Engine 的研究侧代码（ADR-0036）。事件条目登记于 [event-library.md](../../docs/research/event-library.md)。

> 状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。只在合成夹具与冒烟测试上运行过，未在真实数据上校准。

## 分工

| 位置 | 内容 |
|---|---|
| `core/contracts/event.py` | `EventProvider` Protocol 与 5 个 DTO（事件时间 = 可观测时间、溯源、截至 `as_of` 的事件表） |
| `infrastructure/event/` | 执行器 `run_events`（结构性截断 + 相邻检查点一致：不得未来确认；交互的上游规格 / 并集与输入点逐点 lineage 核对，见 `upstream.py`）、输入适配、Event 表逻辑物化 |
| `plugins/events/` | 首批 EventProvider 与交互算子（阈值穿越、波动率突破、状态切换、A 后 B、共现） |
| `research/events/stats.py`（本目录） | 事件表的描述统计：频率、共现、lead-lag、重叠 / 独立性诊断 |

## 统计（`stats.py`）

| 函数 | 输出 |
|---|---|
| `event_frequency(events, start, end, bucket)` | 计数、每日频率、分桶计数 |
| `co_occurrence(a, b, window, start, end)` | 各自计数、A 附近有 B 的次数（及反向）、独立泊松近似下的期望与 lift |
| `lead_lag(a, b, max_lag, bin)` | `B - A` 滞后直方图、A 领先 / B 领先 / 同时的配对数 |
| `overlap_diagnostics(events, horizon)` | 相邻重叠数与比例、贪心不重叠计数（描述性有效样本量）、平均到达间隔、离散度（方差 / 均值²） |

规则：只用 `event_time`（可观测时间）；比例以固定 28 位精度的 `Decimal` 计算；分母为 0 时为 `None`（未定义，不是 0）。
这些数字**只描述、不判定**：不含任何验证阈值，也不是 Validation Profile 的输入（Constitution C-T2 的重叠折算规则由
Profile 定义，本模块只提供描述性计数）。

## Phase 2 接线点

State 序列目前以本地最小形状输入（`infrastructure/event/inputs.py` 的 `StateSeriesPoint` / `inputs_from_state_series`）。
合并 Phase 2 时在该文件增加从 StateProvider 结果到 `StateSeriesPoint` 的适配器（逐点 lineage 须只依赖当时已知的信息）；
事件引擎其余部分不变。

## 已知缺口

- 一个请求对应一个标的；多标的事件表需要 subject 键。
- 执行器默认检查点的成本为 O(检查点 × 可见集合)。
- ✅（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）统计可序列化：`statistic_payload`（时间 ISO-8601 UTC、时长整数微秒、`Decimal` 精确文本）与
  `EventStatsReport`（绑定所统计事件运行的 `result_hash`，`report_hash` 覆盖全部载荷）；事件运行可存取：`infrastructure/event/store.py` 的
  `EventResultStore`（`<root>/<result_hash>.json` 规范 JSON，原子发布、从不覆盖，读取时重建 `EventResult` 复核哈希并要求字节即规范形式）。
  这是**产物存储**，不是数据平面表。
- 物理 Event 表（Iceberg `event.*`）未登记——需先立 ADR（计划执行记录 P3-EVTABLE）；统计未在真实数据上校准；事件频率过低 / 组合爆炸（roadmap 失败模式）尚无自动诊断之外的处理。
