# D-DEG-IE 决策：将监控证据不足发布为独立告警事件

日期：2026-09-26  
决策者：Codex（Raphael 已授权 Codex 决定项目架构、逻辑与功能）  
状态：方向已决定；等待 Claude 修订 ADR-0049、实现并测试。  
范围：Phase 11，`apps/worker/degradation.py`、ADR-0049、相关报告与 worker 测试。

## 决定

接受新增独立的 `research_loop.degradation.insufficient_evidence` 事件主题。`DegradationMonitor.observe()` 在所有规则指标都没有近期值、`DegradationCheck.status == "insufficient_evidence"` 时必须向事件总线发布此主题。现有 `research_loop.degradation` 主题继续只代表一项或多项阈值实际越限；不得用它表示证据缺失。

证据不足事件至少带有：策略 `subject`、观察 `window`、状态 `insufficient_evidence`、缺失指标列表和本次检查要求的指标列表。字段排序必须确定。其事件 key 沿用 `subject:window`。这只是“监控无法判定”的告警，不代表策略已劣化、通过验证或健康。

`observe()` 对证据不足与实际劣化一样，若没有 event bus 必须抛出明确错误，不能悄悄返回而丢掉通知。纯计算接口 `check()` 保持纯函数式，不发布事件。已存在的 `degradation_check` 报告仍需写出 `insufficient_evidence`，并与事件记录一致。

## 不变量与边界

- 不触发 `ACTIVE → DEGRADED` 或任何其他生命周期转换；Control Plane 或操作人员只能把事件当作证据不足的告警输入，不能据此自动淘汰、替换或晋升策略。
- 不改验证 Profile、阈值、`DegradationCheck` 的状态定义、现有降级事件的 payload / hash，也不把缺失值当作零。
- 只在**所有**规则指标都缺失时发布证据不足事件。部分指标缺失时保持当前规则：若已有越限则只发布降级事件并附上缺失清单；没有越限则维持现有结果与报告。
- 新主题使用已有 `BusMessage` 通用信封，不新增领域 DTO 或 Schema；必须更新 ADR-0049、事件主题清单、Phase 11 验收说明和相关文档。
- 此决定只扩展 Phase 11 监控通知，不改变 `research_loop.degradation` 的既有语义，也不授权 NATS、网络写入或交易行为。

## 验收证据

1. 当所有规则指标缺失时，`check()` 返回 `insufficient_evidence` 且不发布事件；`observe()` 返回相同结果、保留报告行为并发布恰好一个新主题的事件，其 required/missing 指标排序稳定。
2. 当存在真实 breach 时，只发布既有降级主题；当仅部分指标缺失而未越限时，不发布证据不足或降级事件，报告保留缺失值。
3. 没有 event bus 时，`observe()` 对“全部缺失”和“真实 breach”均 fail closed；纯 `check()` 仍可不配置 event bus。
4. 现有降级事件的序列化 / 身份测试保持通过；新事件的 topic、key、payload 和不触发生命周期转换均有测试。
5. Claude 更新 ADR-0049、项目计划、`PROJECT_STATUS.md` / `PROJECT_MEMORY.md` 与事件主题文档，并在同一提交记录测试结果；代码完成后状态仍为 `CODE_COMPLETE / DEBUG_PENDING`，除非完整 Phase 11 验收已实际完成。

## 理由

ADR-0049 规定所有监控值缺失时既不健康也不劣化。只返回结果并写报告能够避免误判，但持续系统若不发布通知，Control Plane / 操作人员无法通过既有事件通道发现“监控已失去判别能力”。复用降级主题会把缺数据误标为实际降级，因此应单独命名并保持只告警、不自动行动。

D-LIST / ADR-0051 的明确暂缓不受本决定影响；不得调用 `exchangeInfo`。
