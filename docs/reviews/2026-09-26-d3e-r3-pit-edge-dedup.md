# D3E-R3 PIT 跨日边去重 — 实现说明（fix/d3e-pit-edge-dedup）

**根因**：aggTrade key 的 REST revision 可落在多个 UTC 日；`557774c` 之后每个相关日分区的
`ChannelReconciler.verified_edges` 都返回该 key 的边。`PitSelector._mapped_edges` 逐日映射并直接追加，
同一 Raw `edge_id` 被映射多次（两日样例：4 个映射、2 个唯一 `edge_id`），违反 ADR-0028 §3.2 的一对一映射。
选择结果未变（重复边不产生新 head），但 `PitSelection.edges` 不是按 Raw 边一对一的映像。

**修复**（仅 `infrastructure/pit/selector.py`）：先在固定证据快照下把各日已验证边按稳定 `edge_id` 收集为一份；
同一 `edge_id` 的各份拷贝必须完全相同（`ChannelEdge` 全字段 + 证据表行：端点、表、policy、evidence、
`knowledge_time`、两端 snapshot、投影哈希），否则 `CatalogIntegrityError` fail closed；不同 `edge_id` 从不合并
（不按 observation key 或 revision pair 合并）。之后按 `edge_id` 排序各映射一次。映射、`K_E'`、快照绑定与
Raw 复核路径不变；`channel_reconcile.py`、契约、Schema、ADR 未改。

**验证**（`tests/infrastructure/pit/test_d3e_r3_cross_day.py`）：原失败测试转为通过；新增三日窗口每条 Raw 边
恰映射一次且可复现，以及同一 `edge_id` 在两日内容不一致（knowledge_time / evidence / 被取代端 / 表 / snapshot /
投影哈希）时公开 `select` fail closed。以上新测试在旧 selector 上全部失败。已有强断言未改。
