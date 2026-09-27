# Phase 1 D3E 验收记录：REST revision store 与跨通道 reconciler

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-27 |
| 复核者 | Codex（Raphael 持续授权范围内） |
| 实现者 | Claude Code（Opus） |
| 复核对象 | 候选提交 `45c13d7`；D3E `21e31f5`、R1 `52f7477`、R2 `c326434`、R3 `7e9e084`；跨日修复 `69f0bf0`；PIT 跨日映射 `9baad12` / `50cdc49` |
| 结论 | **PASS — D3E ACCEPTED** |

## 1. 复核范围与结论

复核了 REST 响应与元素 revision 的写入、崩溃恢复、来源证明、跨通道比较和 evidence edge 的持久化复核，并检查 PIT 对跨 UTC 日 edge 的映射。D3E-R3 将每条 REST 行追溯到首次交付的已提交 D3D collection checkpoint，再严格重放并解码原始页；归档行则从已发布归档对象经 D1 严格重新解析。元素行的 native fields 必须逐列等于其 lineage response 正文在 `element_index` 处的元素。Store 与 reconciler 共用 `PersistedRowVerifier`，读取期间固定表头；证据边还按其提交快照重读并核对批次内容。

跨日复核确认：`69f0bf0` 修正 `_verify_edge_provenance` 对跨 UTC 日观察键的证据批次归属核验，完整性规则没有放宽；`50cdc49` 的 PIT 映射按稳定 `edge_id` 去重，多个分区返回同一 edge 时要求完整内容一致，不一致即 fail closed。不同 `edge_id` 保持独立。

本结论只验收 D3E 及其为保持跨日语义所需的 PIT edge 映射修复。Phase 1 后续实现组仍须逐组复核；本记录不代表 Phase 1 已关闭，不合并 `phase/1` 或 `main`，也不开放新的实现批次。

候选分支在 D3E 验收门前已经开发 E/F/G 等后续批次，偏离了 roadmap 的逐批验收顺序。我的处理决定是保留这些提交并继续逐组复核；D3E 验收不追溯批准后续工作，也不降低后续批次的验收标准。若后续复核发现依赖 D3E 的实质缺陷，再按证据修复受影响批次。

## 2. ADR-0027 验收矩阵对照

| ADR-0027 # | D3E 证据与结论 |
|---|---|
| #2–#5 | response / element revision 的身份、同页同字节幂等、异字节保留为另一 revision、重复元素与 competing heads 均由 store 与重放测试覆盖。 |
| #6–#8 | 两种来源到达顺序、四段 cutoff、精确规范投影比较、证据边幂等重放均有 `test_channel_reconcile.py` 覆盖；跨日边和多日 PIT 映射另由跨午夜测试覆盖。 |
| #11–#13 | overshoot、空页、短页、未结束 K 线及拒绝页保留 response 而不产生非法元素；对应 parser / collector / store 测试通过。 |
| #15 | response、element 与 evidence 的提交中断后可由重放恢复；unit / red-team 回归覆盖恢复点。既有全量门禁记录本身未注明 PostgreSQL catalog 设置，本轮不把它描述为 PostgreSQL 专项复跑。 |
| #20–#21 | 双时间继承、REST `arrival_seq` 区间及跨通道 graph 碰撞检查由 store / reconciler 测试覆盖；D2 分配器与身份规则未被 D3E 修改。 |

## 3. 独立代码与回归检查

- `infrastructure/revision/row_integrity.py`：REST response 从首次交付 checkpoint 重建；REST element 与原始页指定索引逐列相等；归档行从来源对象重解析。检查没有发现可绕过来源验证的持久行采用路径。
- `infrastructure/revision/channel_reconcile.py`：REST 与 archive 行在比较前由共享 verifier 证明；跨日 edge 按实际写入分区的键集、批次指纹及固定快照核对。重复、伪造、删除或改时 edge 均拒绝。
- `infrastructure/pit/selector.py`：各日返回的 edge 按 `edge_id` 汇总；相同 ID 内容不一致即拒绝，匹配 edge 仅映射一次。
- 新鲜聚焦运行：`uv run --offline pytest -q -p no:cacheprovider tests/infrastructure/revision/test_rest_store_unit.py tests/infrastructure/revision/test_channel_reconcile.py tests/infrastructure/revision/test_rest_store_postgres.py -m 'not postgres'` → **246 passed, 11 deselected in 132.72s**。
- 新鲜跨日运行：`uv run --offline pytest -q -p no:cacheprovider tests/infrastructure/pit/test_d3e_r3_cross_day.py` → **15 passed in 77.42s**。
- 候选代码的既有严格全量门禁：`GATE OK @50cdc49 | ruff: All checks passed! | 601 files already formatted | Success: no issues found in 463 source files | lock ok | pytest: 5753 passed, 1 warning in 3188.63s (0:53:08)`。当前 `45c13d7` 相对 `50cdc49` 仅有文档变更。

## 4. 限定与后续

- 当前独立环境没有 `HLENS_TEST_CATALOG_URI`；因此本轮没有重新运行 PostgreSQL 集成用例。较早的候选门禁 `8b6fbaf` 在 close-evidence 中明确记录使用真实 PostgreSQL catalog；候选 `50cdc49` 的全量门禁摘要记录 `5753 passed`，但未保留 catalog 配置细节。验收还依据本轮 fresh 非 PostgreSQL 回归。
- E1 及后续 Canonical、PIT、质量、Dataset、Feature、端到端与红队批次仍是 `REVIEW_PENDING`，不得引用本记录宣称已验收。
- 正式 `phase/1` 与 `main` 未更改；后续批次按 review guide 的依赖次序继续独立复核。
