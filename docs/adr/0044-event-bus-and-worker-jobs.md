# ADR-0044: EventBusAdapter 契约、内存总线与 worker 幂等任务（Phase 11 地基）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 11（地基；完整持续研究循环待 P7 / P8 框架后接入） |
| 影响范围 | Contract（`core/contracts/event_bus.py`，additive，Schema 87 → 88）、`infrastructure/event_bus/`、`apps/worker/` |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `EventBusAdapter`：`publish` / `poll(consumer, topic, limit)` / `ack`；**至少一次**投递（未确认即重投），同主题按发布顺序；
   `BusMessage.message_id` = 主题、键与规范化载荷的内容哈希（伪造 ID 被拒绝）。contract suite 覆盖至少一次、消费者相互
   独立与主题内顺序（10-migration.md §2 的"至少一次投递 + 幂等消费测试"）。
2. 首个实现 `InMemoryEventBus`（进程内）；Phase 1～6 不引入 NATS（ADR-0021），外部总线以后按同一 Protocol 与 suite 接入。
3. `apps/worker` 的 `JobRunner`：任务身份 = 名称 + 参数的内容哈希（重复提交只运行一次）；处理失败按 `max_attempts` 重试后记为
   失败（不丢弃）；结果记录后才确认消息，崩溃重投由幂等吸收。worker 不 import `research/`。

## 后果

- 正面：P11 的调度、P7 的批量实验与 P13 的模拟执行可以共用同一总线与任务模型。
- 负面：内存总线不持久；持久化 / 跨进程需要外部总线（另立 ADR，涉及系统软件安装时须 Raphael 授权）。
