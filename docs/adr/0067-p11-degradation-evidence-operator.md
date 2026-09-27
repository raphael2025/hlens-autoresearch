# ADR-0067：Phase 11 显式劣化检查的证据绑定

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | Codex，依 Raphael 对本轮模块开发与决策的明确授权 |
| 相关 Phase | Phase 11 |
| 影响范围 | Research operation、degradation report writer；不改 `core/`、Profile、生命周期规则或 worker 调度 |
| 兼容性 | 只对劣化报告 payload 增加可选 provenance，报告 schema 从 1.0.0 增至 1.1.0；旧 1.0.0 报告继续按原 hash 读取 |

## 背景

ADR-0049 定义按 Validation Profile 阈值比较近期指标与基线；当前 monitor 和 append-only writer 已存在，但 loop 摘要不能充当指标来源。`docs/plans/p11-degradation-operator-spec.md` 提议显式一次性调用，但把 `profile.status == FROZEN` 当作冻结证明。ADR-0062 已将 Profile 的权威冻结来源定义为追加式登记及目录外锚点，单看对象状态不足以证明已冻结。

## 决策

1. 保留显式、单次、本机、只读的 operation。调用方必须提供 subject、LifecycleHistory、ValidationProfile、其 PASS ValidationReport、基线指标及明确 gate 映射、近期指标清单、聚合方法身份、UTC 半开时间窗、ProfileFreezeRegistry 记录与所需外部锚点，以及报告目录。不得从 loop 摘要、最近文件、当前时钟、默认阈值或网络推导输入。
2. operation 验证生命周期历史属于同一 subject 且重放终态为 ACTIVE；报告绑定该历史的内容哈希。它只证明“调用方提供的历史以 ACTIVE 结束”，不声称该历史是最新权威记录。当前仓库没有可信的最新生命周期 resolver；以后若要声称“当前 ACTIVE”，需单独设计并实现权威 resolver。
3. baseline report 必须属于同一 subject、引用完全相同的 Profile ref/hash 且 verdict 为 PASS。每个 Profile 要求的劣化指标必须恰好映射到一个同名 Validation gate，并使用其明确的精确值；不得模糊匹配、平均、选最佳 gate 或补缺省值。baseline 指标键须与去重后的 Profile metric 名称完全相等；未使用的额外 metric 被拒绝。若 Profile 对同一 metric 名称声明多个方向 / 阈值键，operation 拒绝歧义输入。
4. Profile 必须同时满足对象状态 `FROZEN`，且存在 ADR-0062 规定的、与 Profile ref/hash 匹配的有效 freeze registration 和必需目录外锚点。只验证调用方显式提供的记录；不声称具名批准人字段认证了真实身份。没有登记或锚点即拒绝并不写报告。登记为空时这是预期 fail-closed 行为。
5. Recent metrics 必须绑定同 subject、Profile ref/hash、相同半开窗口、非空方法身份与内容哈希 observation manifest。每个来源的 event time 必须落在 `[start, end)`，observation time 不得晚于 `end`（`end` 对 event time 是排除边界，对 observation time 是包含的 as-of 截止）。P11 本地 manifest 身份为 `hlens.p11.recent-metric-manifest@1.0.0`。operation 不负责把 raw observations 聚合成指标，也不判定源数据真实性；报告内嵌完整 manifest（source ids / hashes / 时间、指标值、方法身份），供审阅者独立复算内容哈希。source id 是调用方声明的引用，不是仓库 resolver 或真实性证明；是否能据此重新取得外部原件取决于调用方的外部存储约定。不得将 loop usage、stage summaries 或交易数据自动转换为指标。
6. 继续使用 `DegradationMonitor` 的精确 Profile 阈值与三态结果。缺失指标保持缺失；证据不足不等于健康；任何异常、冲突或来源绑定失败均不产报告。operation 不发布 event、不改生命周期、不排程、不运行 trial。
7. `ProfileFreezeRegistry` 提供只读 `anchor_snapshot`（重新读取并验证外部 anchor journal 的记录数与 head hash；若实例打开后被外部修改则拒绝），不暴露 anchor 路径。`degradation_check` 报告新增可选 `evidence` 对象，包含 lifecycle history hash、freeze record id / calibration report hash / anchor snapshot identity、baseline report hash、Profile ref/hash、recent observation manifest id/hash 与完整 manifest、aggregation method id、window 起止与 baseline gate 映射。该对象进入 `check_hash`。schema 版本提升至 1.1.0；旧 1.0.0 payload / hash 原样兼容，不重写旧文件。1.1.0 只能由 operation result 生成，通用 legacy payload builder 不接受任意 evidence mapping；此边界防止常规误用，不是对恶意 Python 调用者的签名认证。
8. 报告仍由现有只追加 writer 写入；相同完整输入幂等，路径冲突或同 hash 不同内容时拒绝。API 仅继续只读读取与校验，不新增写端点。

## 实施边界

实现限于 `research/operations/degradation.py` 的本地 value objects / operation、`research/reports/degradation.py` 的 additive evidence serialization、`ProfileFreezeRegistry.anchor_snapshot` 只读读取，以及只读报告解析和相关文档。不得修改 `core/contracts/`、`core/domain/`、Schema snapshots、Constitution、生命周期迁移、Validation 门、Profile 数值、worker 自动调度或 API 写入路径。报告 schema 的兼容读取若需改变，须先保持 1.0.0 的旧 hash 复现。

## 未解决 / 明确不作声称

- 本 ADR 不定义近期指标的金融含义、计算方法或真实来源；方法与输入由调用方显式声明，报告只证明内容绑定。报告内嵌输入 manifest，但没有 source URI resolver，也不认证 caller 声明的 source id、来源或聚合真实性。
- 本 ADR 不证明传入的生命周期历史是最新权威记录，也不认证人类审批人身份。
- ProfileFreezeRegistry 当前为空且 Profile 数值未冻结；对应输入缺失时 operation 必须拒绝。这不影响 monitor 的独立研究调用，也不构成 Phase 11 验收。
- 不定义 CLI、scheduler、自动 worker launcher、事件发布或任何交易能力。

## 结果

实现后可用明确输入和报告 provenance 逻辑；实现标记 `CODE_COMPLETE / DEBUG_PENDING`，后续统一验收。当前实现位于 Codex 协调 worktree，尚未进入本地 `main`。旧劣化报告的身份保持不变。
