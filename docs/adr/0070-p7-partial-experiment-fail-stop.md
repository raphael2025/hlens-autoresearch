# ADR-0070：P7 批次中途失败后停止并要求人工审查

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | Codex，依 Raphael 对模块开发与决策的明确授权 |
| 相关 Phase | Phase 7、Phase 11 loop host |
| 影响范围 | `apps/worker/loop.py`；不改 LoopRecord / checkpoint 格式、Schema、TrialLedger 或生命周期契约 |

## 背景

`ExperimentStage` 逐个执行批次 trial，但只有整批成功后才把 outcomes 写入 `ResearchMemory`。一个较晚 trial 抛错时，较早 trial 可能已推进生命周期或追加 TrialLedger 条目；worker 将 experiment stage 记为 `FAILED`，跳过 validation / memory，随后仍把失败轮的部分 ledger / lifecycle 状态写入 checkpoint 与 audit。重开时这些已发生的 trial side effects 仍可见，但没有对应的完整 `TrialOutcome`；下一轮可能以相同 attempt 再跑，造成无新计数的重复执行。

ADR-0049 当前允许一般阶段失败后继续下一轮，但上述 experiment 批次不是无状态失败。单纯逐个保存 outcome 也不安全：失败轮的 validation / memory 阶段会跳过，且重启恢复的 outcome 不含执行输入对象。

## 决策

1. **失败的 experiment stage 设置持久可推导的人工审查状态。** 如果一条已记录 `LoopRecord` 中 `stage.name == "experiment"` 且 `stage.status == FAILED`，ResearchLoop 设置 `recovery_required`，不提交任何后续 round。当前进程停止；重开同一 audit 时，从最后一条已记录 LoopRecord 的 stage 状态重建相同停止状态。
2. `run_unattended` 在已有 `recovery_required` 时正常返回已记录的轮次，不调度下一轮。直接提交或消费额外 round job 时，`_check_can_run` fail closed，JobRunner 不会再次执行 trial。
3. 该停止状态**不改变 `LoopRecord` 字段、载荷、`record_hash`、checkpoint 格式、TrialLedger 或生命周期迁移**。仍用已有的 `FAILED` StageStatus / RoundStatus；恢复标记从 hash-bound audit 中确定性推导，不写墙钟时间或旁路状态文件。
4. 仅当**最后一条记录**的 experiment stage 为 FAILED 时阻止续跑。既有 audit 中失败后又有后续记录的历史不被追溯拒绝；新代码不会继续产生这种序列。
5. 人工审查须检查 round record、lifecycle transitions、TrialLedger 及持久 checkpoint，并明确决定后续处理。现有版本不自动补 outcome、不重试原 attempt、不回滚追加日志；自动逐 trial 恢复须另立 ADR，覆盖 experiment、validation、memory 阶段的 side effects 和崩溃点。
6. 其他阶段失败继续遵守 ADR-0049 原规则。中断但未记录的 started round、checkpoint / audit 写入失败仍按现有 durable loop 规则停止并要求审查。

## 后果

- 试验批次中途失败不会在同一 audit 下静默开始下一轮，也不会自动重复已计入 ledger 的 attempt。
- 状态对旧 `LoopRecord` 字节兼容；Web / API 不需要新的 round status。
- 本决定提供 fail-stop 边界，不是自动恢复方案。失败轮之前已发生的 lifecycle / ledger side effects 仍需人工处理，当前没有通用的人工修复 CLI。
- 定向测试和 Phase 7 / 11 验收留到 Raphael 指定的后续验收阶段。

## 实施状态

`ResearchLoop` 在已记录的失败轮后设置 `recovery_required`，并在重开时从最后一轮恢复；实现位于 Codex 协调 worktree，未运行测试，待后续验收。
