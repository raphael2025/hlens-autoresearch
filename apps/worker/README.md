# apps/worker

异步任务执行：采集、计算、实验运行、验证。任务必须幂等可重试。事件总线通过 EventBusAdapter 访问（NATS 引入时机见待决 D-10）。

> 框架已实现（ADR-0044，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`jobs.py` 的 `JobRunner`（内容寻址任务 ID、至少一次消息 + 幂等执行、有界重试、失败记录不丢弃），总线为 `infrastructure/event_bus/InMemoryEventBus`。
