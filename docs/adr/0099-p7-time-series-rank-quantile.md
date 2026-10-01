# ADR-0099: P7 transformation 之 `rank` / `quantile` 的时间序列语义

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-30；关闭 ADR-0082 最后两个 OPEN 项）；§5/§6 被 ADR-0100 §1/§2 修订 |
| 日期 | 2026-09-30 |
| 决策者 | Claude Code（PM，依 Raphael 2026-09-28 正式授权，CLAUDE.md §0） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 7（Automated Hypothesis Discovery） |
| 影响范围 | `research/hypotheses/typed_plan.py`、`typed_plan_lowering.py`；不改 `core/` |
| 是否破坏兼容 | 否（plan 格式 1.1.0 → 1.2.0，additive；旧计划照常解析） |

## 背景（Context）

ADR-0082 已接受 `standardize` / `difference` / `smooth` 三个时间序列变换，`rank` / `quantile` 仍为 OPEN，原因是横截面语义未定：统计总体是什么、跨标的如何对齐都不在当时范围内。横截面版本需要 universe 快照与时间对齐契约，而现有 `FeatureSpec` 只描述单标的特征。

## 决策（Decision）

1. **只接受时间序列语义**，与 ADR-0082 已接受的三个变换同构：显式正整数 `window`（bar 数）、只向后看、滚动窗口即拟合范围（C-L3）、任一 bar 缺值时输出缺失并向前传播（不填零 / 不插值 / 不前向填充）。
2. **`rank`**：输出当前 bar 的值在“含当前 bar 的最近 `window` 根 bar”中的百分位秩，取值 ∈ [0, 1]，计算式为 `(count_less + 0.5 × (count_equal − 1)) / (window − 1)`（平局取平均秩）。`window` 必须 ≥ 2。definition 为 `p7.transformation.rank_ts@1.0.0`，params 除 ADR-0082 的公共声明外，另加 `ties=average`、`scale=unit_interval`。
3. **`quantile`**：输出 `floor(rank × buckets)` 并截断到 `[0, buckets − 1]` 的整数桶号，其中 `rank` 按第 2 条计算。新增 plan 节点参数 `buckets`（正整数，≥ 2），**仅 `quantile` 必填**，其他 transform 出现该参数时拒绝。definition 为 `p7.transformation.quantile_ts@1.0.0`，params 另加 `buckets`、`ties=average`。
4. `typed_plan.py` 的 `PLAN_FORMAT_VERSION` 升到 1.2.0；1.1.0 计划照常解析，但 1.1.0 计划中的 `rank` / `quantile` 节点仍按旧语义 `operator_open`（不得回溯改变旧计划的含义）。
5. **横截面 rank / quantile 仍不在范围内**，将来需要另立 ADR 定义 universe 快照、对齐与总体。
6. 授权范围与 ADR-0082 相同：只授权纯的、不可运行的 lowering。不实现、不登记 Provider；`TypedPlan.runnable` 与 `compile_plan` 的拒绝行为不变。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 横截面语义 | 符合常见因子用法 | 需要 universe / 对齐契约（H1） | 留待后续 ADR |
| `quantile` 固定默认桶数 | 无需新参数 | 违反“不猜默认值” | 要求显式 `buckets` |

## 后果（Consequences）

- 正面：六类算子的 lowering 全部定义完毕，基础 lowering 不再对 transformation 返回 `operator_open`。
- 负面 / 代价：plan 格式升一个 minor 版本。
- 需要迁移的内容：无。
- 对复现性的影响：旧计划哈希与含义不变。

## 合规检查

- [x] 不破坏已冻结契约
- [x] 不修改 Validation Constitution
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变
