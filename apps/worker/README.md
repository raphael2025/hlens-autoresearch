# apps/worker

异步任务执行：采集、计算、实验运行、验证。任务必须幂等可重试。事件总线通过 EventBusAdapter 访问（NATS 引入时机见待决 D-10）。

> Architecture Bootstrap：目录仅作规划，**尚无代码**。实现需等待对应 Phase 开启（见 docs/research/roadmap.md）。
