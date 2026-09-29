# ADR-0093: Explicit trusted Worker runtime factory

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-29 |
| 决策者 | Codex，依本项目当前目标授权的工程决策权 |
| 起草者 | Codex |
| 相关 Phase | Phase 1（W2）、运行时 W10 |
| 影响范围 | Infrastructure / Security |
| 是否破坏兼容 | 否 |

## 背景（Context）

W2 要求 Worker 有独立生产进程入口，并验证启动、优雅停止和重启恢复。现有 `JobRunner` 与
FileEventBus 已提供持久化结果、单写者约束和崩溃恢复，但 `tests/apps/worker_jobs_child.py` 是测试工具，
不是生产入口。依据 [ADR-0044](0044-event-bus-and-worker-jobs.md)，结果必须先于 ack 持久化；依据
[ADR-0049](0049-continuous-research-loop.md)，`apps/worker` 不得导入 `research/`，研究阶段由研究侧组合根注入。

## 决策（Decision）

1. Worker 以 `python -m apps.worker.serve --factory package.module:callable` 启动。Factory 是部署方提供的
   受信代码入口，返回 context manager；进入后提供完整配置的 `JobRunner`，退出时释放 bus 等资源。
   Host 不根据 job/message/topic 动态选择 handler，不发现任意 handler 插件，不导入 `research/`。
2. Host 固定单进程、单消费者，每次调用 `run_pending(limit=1)`。队列空时等待 1 秒；停止信号可唤醒等待。
3. SIGINT / SIGTERM 只请求停止：空闲时不再开始 poll；任务执行期间允许当前一次 `run_pending(1)` 完成
   结果 journal 与 ack 边界后退出。Host 不取消 handler；有限退出时限由外部 supervisor 的 grace timeout 提供。
4. 重启由外部 supervisor / 操作者执行，并由 Factory 重建相同的 bus 和结果 journal 配置。沿用 ADR-0044：
   已有结果只 ack；未完成任务仅在显式 `idempotent=` 时按既有规则重跑，否则 fail closed。
5. Factory 导入/构造失败、Journal 损坏、`JobInterrupted` 或运行时基础设施异常导致非零退出；正常任务失败
   若已按 `JobRunner` 重试耗尽并持久化结果，则按现有语义 ack，不单独使进程失败。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 受信 Runtime Factory | 组合边界明确，复用现有 Runner，不扩展 handler 发现面 | 部署方需提供 Python factory | 选择：当前 W2 最小完整组合边界 |
| 命名 handler plugin registry | 插件发现统一 | 扩大受支持扩展面与安全契约，需单独设计 | 当前没有该能力需求，超出 W2 |
| 延后生产入口 | 不新增运行面 | W2/W10 启动和关停无法验收 | Worker 是已批准工作包，无法完成其验收 |

## 后果（Consequences）

- 正面：启动配置由单一受信组合根提供；通用 host 与 research 保持依赖方向；关停有清晰的任务边界。
- 负面 / 代价：同步 handler 无法由 host 强制超时；部署必须管理 factory 与 supervisor。
- 需要迁移的内容：无；测试专用子进程 harness 保留。
- 对复现性的影响：无；job journal、身份和恢复规则不变。

## 合规检查

- [x] 不破坏已冻结契约
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- [ADR-0044](0044-event-bus-and-worker-jobs.md)
- [ADR-0049](0049-continuous-research-loop.md)
- [Worker runtime decision packet](../reviews/2026-09-29-worker-runtime-composition-decision.md)
