# ADR-0032: 历史归档的事件时间可用性假设（PIT 叠加层，D-HIST）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | **Raphael**（2026-09-25 对 D-HIST 回复"同意推荐方案"） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 1（使历史研究成为可能；影响 F3 数据集与 F4 特征） |
| 影响范围 | PIT 选择（ADR-0023 §5 的解释）；不改存储、不改契约 |
| 是否破坏兼容 | 否：不绑定该假设的规格结果逐位不变 |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md) §2 / §5、[ADR-0028](0028-dual-raw-canonical-lineage.md)、研究宪法 C-L1 |

## 背景

官方资料不能证明任何历史行情 revision 的公开时刻（`docs/architecture/evidence/binance-spot-publication.md`），
所以 `binance.spot.publication@1.0.0` 一律保守取 `available_time = ingest_time` 并写证据缺口；已发布契约 2.0.0 也规定
"早于 ingest 必须有证据，不得仅凭 event_time 回填"。结果是：早于本机采集的历史**不能用于任何历史回测**。
这触及宪法 C-L1 的前提，由 Raphael 本人决定。

## 裁决

1. **存储不变**：Raw 与 Canonical revision 的 `available_time`、证据缺口、契约与已提交数据一律不改；默认 PIT 行为不变。
2. **假设政策** `hlens.availability.archive-event-time-assumption@1.0.0`（role = availability）只能由数据集的
   `PointInTimeSpec.availability_bindings` **显式绑定**才生效（版本与哈希必须完全一致，否则拒绝）：
   - 适用对象：lineage 来自**归档元素表**（`raw.binance_spot_agg_trades`、`raw.binance_spot_klines_1m`）且证据缺口是
     `binance.spot.publication@1.0.0` 的成交 / K 线"公开上界未声明"缺口的 Canonical revision；
   - 有效可用时间 = `min(存储的 available_time, 可观察时刻 + 5 秒)`，可观察时刻为瞬时事件的 `event_time`、区间事件的
     `event_end_time`（K 线收盘）；**从不晚于**存储值；
   - 依据：官方 WebSocket 文档称成交流"Real-time"、K 线流每 2000ms 推送；**明示假设**：归档内容等于当时实时推送的内容；
     5 秒为保守延迟；
   - 不适用：REST 补尾、归档 revision 本身、上市记录、任何其他缺口；知识轴（`knowledge_time`、`knowledge_cutoff`）不变。
3. PIT 在候选过滤与区间变化点上使用有效可用时间；`PitSelection` 公开 `assumed`（revision → 存储值, 有效值）并在
   `selected_rows` 中给出有效值，下游（重采样、特征、数据集跨度）一致使用；**证据缺口照常列出**并随 manifest 绑定，
   manifest 通过其 PIT 规格绑定该假设政策（名称、版本、哈希）。
4. 归档替换（同一路径不同内容）仍无法排序：competing heads，数据集 fail closed——修正后的数据不会借假设回填到过去。

## 备选方案

| 方案 | 内容 | 结论 |
|---|---|---|
| **A（采纳）** | PIT 叠加层，数据集显式绑定，存储不变 | Raphael 批准 |
| B | 维持现状，只做采集之后的前向研究 | 拒绝：项目无法做历史研究 |
| C | 改写存储的 `available_time` | 拒绝：把假设伪装成证据，违反已发布契约 |

## 后果

- 正面：绑定假设的数据集可以在历史区间构建；假设可见、可复现、可替换（新版本）。
- 负面：研究结论依赖"归档 = 实时推送"的假设；验证与报告必须显示所用假设（manifest 已绑定）。
- 义务：Phase 4 验证流水线须能按是否绑定该假设分组报告结果。

## 合规检查

- [x] 不修改 Domain Contract（记录对象不变；有效时间只在 PIT 内部计算）
- [x] 宪法 C-L1 仍成立：选择只用 `有效 available_time ≤ t`，有效值由版本化、被绑定的政策给出
- [x] 由 Raphael 本人批准（红线）
