# research/loop

Phase 11 持续研究循环的**研究侧**（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

通用机制（调度、预算、生命周期护栏、审计、事件发布）在 `apps/worker/loop.py`；本目录只提供阶段实现与组合根，
依赖方向 research → apps/worker（反向禁止）。

| 文件 | 内容 |
|---|---|
| `stages.py` | 六个阶段：`IngestStage`（每轮新的合成市场段）、`StateStage`（效率比趋势 / 震荡摘要）、`HypothesisStage`（知识假设 + 已人工审阅的 LLM 草稿预登记；新 LLM 草稿只入审阅队列）、`ExperimentStage`（`lag_minutes = k` 的滞后符号跟随研究）、`ValidationStage`（G2 有效样本 + G3 多重检验校正 p 的筛查，阈值取自 Profile）、`MemoryStage`（REJECTED / FAILED 写入 Failure Registry 与生命周期） |
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、实验与状态记录）；`ReviewQueue` 只能由人批准 |
| `compose.py` | `SyntheticLoopConfig` + `build_synthetic_loop`：研究侧组合根，所有数字来自配置 |

要点：

- 每次登记都是一次 trial（含失败），G3 用该假设族的累计 trial 数做多重检验校正（Constitution C-T1）。
- 筛查 PASS **不**推进生命周期（留在 VALIDATION 等待完整 P4 / P8 流水线）；循环永远到不了 PAPER / ACTIVE。
- 合成市场的植入真值不作为输入，只在审计摘要中计数；合成结果不支持真实市场结论。

未完成（调试批次）：接入 Phase 2 `StateProvider` 执行器、Phase 4 完整验证流水线与 `ExperimentRun` 复现元组、持久化审计与 NATS、研究仪表盘。
