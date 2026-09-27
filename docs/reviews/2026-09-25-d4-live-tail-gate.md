# Phase 1 D4 门记录：WebSocket live tail 不启用

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 起草者 | Claude Code（Opus），依 Raphael 2026-09-25 的持续执行授权 |
| 复核者 | Codex，2026-09-27 |
| 基线 | D3E 已由 Codex 于 2026-09-27 接受（候选 WIP `9f77665`；记录 `2026-09-27-d3e-acceptance.md`） |
| 决定 | **D4 CLOSED：Phase 1 首切片不启用 WebSocket live tail** |
| 冻结依据 | [ADR-0022](../adr/0022-phase1-market-and-execution-scope.md) 数据源表"Live tail（WebSocket）"行与其验收矩阵第 8 项；[roadmap](../research/roadmap.md) Phase 1 验收矩阵 #14 |

## 1. 规则

ADR-0022 把 live tail 定为**延后**：只有历史 backfill、REST gap reconciliation 与重放幂等验收**全部通过后**才可加入；
其验收矩阵第 8 项把"在此之前启用"列为不允许。roadmap #14 同样要求"只在三项验收后启用"，并明确**可不启用**。
因此 D4 只有两种合法结果：三项全部已验收且确有消费者时，先冻结设计再启用；否则记录不启用。

## 2. 三项前置的实际状态

| 前置 | 证据 | 状态 |
|---|---|---|
| 历史 backfill | D0 下载壳（`7a9f468`，门 `6652452`）、D1 parser（`c966085`，门 `8ff479d`）、D2 revision store（`ba9f417` + `b05486b`，门 `3145980`）均经 Codex 验收 | **只在小型 fixture 与两个单位边界日的只读 smoke 上满足**。大体量 BTC 日归档的内存 / 吞吐基线与可恢复批量 backfill 尚未做（`PROJECT_STATUS.md` §7 风险、G3 容量基线） |
| REST gap reconciliation | D3A～D3E（含 R1～R3 与跨日 provenance 修复）已由 Codex 验收；记录 `2026-09-27-d3e-acceptance.md` | **已满足**：D3E store / reconciler 已验收 |
| 重放幂等 | D2 归档与 D3D collector 重放已验收；D3E 的 REST store / reconciler 重放、崩溃恢复已由 Codex 验收 | **已满足** |

结论：D3E 已满足 REST gap reconciliation 与 REST 重放前置，但历史 backfill 的真实规模容量基线 / 可恢复批量流程尚未完成。因此三项前置仍**没有全部通过**；ADR-0022 与 #14 允许不启用，Phase 1 当前也没有已批准的实时消费者，故 D4 关闭且不增加 WebSocket 能力。

## 3. 为什么"不启用"也是正确的范围选择

1. **无消费者**：Phase 1 的关闭条件（#15～#20）是 Canonical、PIT、manifest 与 Representation，全部基于有证据的历史数据；
   没有任何已批准的批次需要亚分钟级的实时数据。以"以后可能需要实时"为由启用，没有仓库证据支持。
2. **新增语义面大**：WebSocket 需要自己的 source identity、消息与 checkpoint 模型、断线 / 重订阅、消息去重与乱序、
   运行时缺口修复、availability 与重放语义。这些都不在 ADR-0022 / 0023 / 0027 的冻结正文里，
   在 Canonical / PIT 尚未闭环前引入，会把未验证的长连接状态带进 Raw。
3. **不改变冻结语义**：延后不改表、不改身份、不改 policy；REST 补尾（D3）已覆盖归档之后的尾部，
   历史可复现性（ADR-0022 方案 A 相对方案 B 的理由）保持不变。

## 4. 本批没有做的事

- 没有编写任何 WebSocket 代码、依赖、设置、Schema、契约或表；
- 没有修改任何 Accepted ADR 的历史决定；
- 本门不实现 WebSocket。候选分支中已存在的 E/F/G 实现是独立待复核批次；它们不改变 D4 关闭结论，也不因本次复核被接受。

## 5. 将来重新开启的前置

只有同时满足以下各项，才可另起批次（并先写 Proposed ADR）考虑 live tail：

1. D3E（含 R1～R3）已由 Codex 验收；未来若要重新考虑 live tail，仍须先完成真实规模历史 backfill 容量基线与可恢复 checkpoint（G3）；
2. 出现一个已批准、确实需要实时数据的消费者（最早是 Phase 10 paper 执行或 Phase 11 持续研究循环），并写明延迟要求；
3. 冻结 WebSocket 设计：source identity 与版本、消息 / 批次 checkpoint、断线与重订阅、去重与乱序、
   与归档 / REST 的通道 precedence（沿用 D-33 的"精确投影相等才写证据边"或新 ADR）、`arrival_seq` 区间、
   availability / knowledge 时间、重放与崩溃恢复、market-data-only 端点 allowlist 与安全边界；
4. Phase 13 之前仍只允许公共行情流，不得接触账户 / 用户数据流、listenKey 或任何交易端点。

## 6. 验收矩阵对照

| roadmap # | 本批证据 |
|---|---|
| #14 | 启用条件逐项检查（§2）：未全部满足 → 不启用；未启用本身被 #14 明确允许 |
| #21 | 本次 Codex 复核只更新文档；`tests/test_docs_consistency.py` + `tests/test_architecture_boundaries.py`：18 passed；`git diff --check` 通过 |


## 7. Codex 复核与结论（2026-09-27）

Codex 复核确认 ADR-0022 将 live tail 延后，roadmap #14 明确允许不启用；D3E 虽已验收，但真实规模历史 backfill 容量与可恢复批量流程仍未通过验证，且当前没有已批准的实时消费者。故接受 D4 的“不启用”门：**D4 CLOSED，Phase 1 不启用 WebSocket live tail**。这不批准未来启用，也不批准 E/F/G 后续实现。若未来出现实时消费者，须完成 §5 前置并另立 Proposed ADR 后再审。
