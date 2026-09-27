# Phase 1 D4 门记录：WebSocket live tail 不启用

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 起草者 | Claude Code（Opus），依 Raphael 2026-09-25 的持续执行授权 |
| 复核者 | **待 Codex**。本记录是 `REVIEW_PENDING` 提案，不是验收 |
| 基线 | D3E-R2 `c326434`（`REVIEW_PENDING`，叠加在 D3E `21e31f5`、D3E-R1 `52f7477` 之上；最后接受点 D3D 门 `300bf33`） |
| 决定（提案） | **D4 CLOSED：Phase 1 首切片不启用 WebSocket live tail** |
| 冻结依据 | [ADR-0022](../adr/0022-phase1-market-and-execution-scope.md) 数据源表"Live tail（WebSocket）"行与其验收矩阵第 8 项；[roadmap](../research/roadmap.md) Phase 1 验收矩阵 #14 |

## 1. 规则

ADR-0022 把 live tail 定为**延后**：只有历史 backfill、REST gap reconciliation 与重放幂等验收**全部通过后**才可加入；
其验收矩阵第 8 项把"在此之前启用"列为不允许。roadmap #14 同样要求"只在三项验收后启用"，并明确**可不启用**。
因此 D4 只有两种合法结果：三项全部已验收且确有消费者时，先冻结设计再启用；否则记录不启用。

## 2. 三项前置的实际状态

| 前置 | 证据 | 状态 |
|---|---|---|
| 历史 backfill | D0 下载壳（`7a9f468`，门 `6652452`）、D1 parser（`c966085`，门 `8ff479d`）、D2 revision store（`ba9f417` + `b05486b`，门 `3145980`）均经 Codex 验收 | **只在小型 fixture 与两个单位边界日的只读 smoke 上满足**。大体量 BTC 日归档的内存 / 吞吐基线与可恢复批量 backfill 尚未做（`PROJECT_STATUS.md` §7 风险、G3 容量基线） |
| REST gap reconciliation | D3A～D3D 经 Codex 验收（ADR-0027 门 `2e40b36`、D3B `0c3af31`、D3C `960b552`、D3D `300bf33`）；D3E store / reconciler `21e31f5` → R1 `52f7477` → R2 `c326434` | **未验收**：D3E 全链路仍 `REVIEW_PENDING`，Codex 两轮复核都退回过已存行完整性缺陷 |
| 重放幂等 | D2 归档重放、D3D 同 `request_id` 零网络重放已验收；D3E 的 REST store / reconciler 重放与崩溃恢复有测试，但随 D3E 一起未验收 | **部分满足**：REST 写入侧未验收 |

结论：三项前置**没有**全部通过。按 ADR-0022 与 #14，本阶段不能启用 live tail；这不是偏好，而是冻结规则的直接结果。

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
- 没有开放 E0 / E1：Canonical 设计门仍关闭，待 Codex 验收 D3E 与本记录后开放。

## 5. 将来重新开启的前置

只有同时满足以下各项，才可另起批次（并先写 Proposed ADR）考虑 live tail：

1. D3E（含 R1 / R2）经 Codex 验收；历史 backfill 在真实规模上完成容量基线与可恢复 checkpoint（G3）；
2. 出现一个已批准、确实需要实时数据的消费者（最早是 Phase 10 paper 执行或 Phase 11 持续研究循环），并写明延迟要求；
3. 冻结 WebSocket 设计：source identity 与版本、消息 / 批次 checkpoint、断线与重订阅、去重与乱序、
   与归档 / REST 的通道 precedence（沿用 D-33 的"精确投影相等才写证据边"或新 ADR）、`arrival_seq` 区间、
   availability / knowledge 时间、重放与崩溃恢复、market-data-only 端点 allowlist 与安全边界；
4. Phase 13 之前仍只允许公共行情流，不得接触账户 / 用户数据流、listenKey 或任何交易端点。

## 6. 验收矩阵对照

| roadmap # | 本批证据 |
|---|---|
| #14 | 启用条件逐项检查（§2）：未全部满足 → 不启用；未启用本身被 #14 明确允许 |
| #21 | 本批只改文档；全量 pytest / ruff / format / mypy / `uv lock --check` 与静态扫描见提交说明 |
