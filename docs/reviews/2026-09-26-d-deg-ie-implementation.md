# D-DEG-IE 实施说明：监控证据不足的独立告警事件

日期：2026-09-26
实施者：Claude Code（Opus）
决策：Codex，`docs/reviews/2026-09-26-d-deg-ie-codex-decision.md`（分支 `codex/full-code-review-2026-09-26`）
分支：`claude/phase11-insufficient-evidence`，基于 WIP `1cd3284`
状态：CODE_COMPLETE / DEBUG_PENDING（等待完整 Phase 11 验证；未合并进 `wip/all-code-completion` / `phase/1` / `main`）

## 改动

- `apps/worker/degradation.py`：新增 `INSUFFICIENT_EVIDENCE_TOPIC = "research_loop.degradation.insufficient_evidence"`。
  `DegradationMonitor.observe()` 在所有规则指标都缺近期值、`status == "insufficient_evidence"` 时发布恰好一条该主题事件，
  不发布 `research_loop.degradation`。payload：`subject`、`window`、`status`、`missing`（排序）、`required`（规则指标，排序）；
  key `subject:window`；信封为既有 `BusMessage`。需要发布而没有 bus → `ValueError`（与实际越限相同，fail closed）。
  `check()` 仍是纯函数。实际越限的主题、payload 与消息身份不变；部分缺失无越限不发布（行为不变）。
- `tests/apps/test_research_loop.py`：全部缺失的发布与精确信封 / payload、payload 排序确定性、无 bus 拒绝（两种情形）且
  `check()` 纯、越限只发既有主题、部分缺失不发布、不触发生命周期（import / payload 检查）。原先断言“全部缺失不发布 / 无 bus 也返回”
  的两处按决定改写为新行为。
- `docs/adr/0049-continuous-research-loop.md`：第 7 条补一句；新增 “Implementation note (D-DEG-IE …)” 含事件主题清单。
- `apps/worker/README.md`：`degradation.py` 条目改为新行为。

## 不变量

- 不触发 `ACTIVE → DEGRADED`、替换、批准或任何生命周期转换；两个主题都只是告警 / 证据。
- 不改验证 Profile、阈值、`DegradationCheck` 状态定义、`degradation_check` 报告、既有降级事件 payload / hash；缺失值不当作零。
- 不新增领域 DTO / Schema；不涉及 NATS、网络写入、交易；不调用 `exchangeInfo`（D-LIST / ADR-0051 暂缓不受影响）。

## 未在本分支更新（留给主集成同步）

按本批次指示，隔离分支不改共享的计划 / 状态 / 记忆文件：`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、
`docs/plans/2026-09-26-all-code-completion-plan.md`、`docs/reviews/2026-09-26-autonomous-decisions.md`（其 D-DEG-IE 行仍写“不发布，
待 Codex 决定”）。主集成复核后同步。
