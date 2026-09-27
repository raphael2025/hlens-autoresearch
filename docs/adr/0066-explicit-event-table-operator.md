# ADR-0066: 独立、显式的 Event 表操作入口

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-27 |
| 决策者 | Codex（依 Raphael 对本项目模块完成与技术决策的明确授权） |
| 起草者 | Codex |
| 相关 Phase | Phase 3 — Event & Interaction Engine |
| 影响范围 | Infrastructure / Operations；不改契约、Schema、Phase 1 表定义或启动流程 |
| 是否破坏兼容 | 否 |

## 背景

[ADR-0056](0056-event-table.md) 已定义 `event.events` 及显式函数 `ensure_event_tables(adapter)`，但没有独立的操作入口。
Phase 1 的 15 张表与 Phase 3 的 Event 表必须保持分离；生产 catalog 建表也必须是操作人员明确发起的单独步骤。
ADR-0056 中“不接入任何 provisioning 脚本”容易被理解为禁止任何独立操作命令，与“生产建表是单独运维步骤”的决定缺少可执行入口。

## 决策

新增独立操作命令 `python -m infrastructure.event.create_event_tables`，并明确解释 ADR-0056 的限制：该命令不是 Phase 1 或通用 provisioning 的一部分，也不会由应用启动、worker 或其他建表命令调用。

- 默认运行只显示安全的使用说明，不读取设置、不连接 catalog、不创建表。
- 只有显式传入 `--apply` 才加载运行设置并连接 catalog。
- 操作入口显式组合 `PHASE1_TABLES + PHASE3_TABLES` 作为适配器 registry，然后只调用 `ensure_event_tables(adapter)`；该函数只创建 / 校验 `PHASE3_TABLES`。
- 不修改 `PHASE1_TABLES`、`PHASE1_REGISTRY`、Phase 1 golden 哈希、`infrastructure/catalog/create_phase1_tables.py` 或任何自动启动流程。
- 默认实现和测试不得连接真实 catalog；本 ADR 不授权运行该命令或修改任何数据库。实际生产建表仍须另行取得 Raphael 的操作授权。
- 输出只列出 `event.events` 的定义绑定与创建状态，不回显 catalog URI、DSN、用户名、密码或连接异常文本中的凭据。

## 备选方案

| 方案 | 内容 | 结论 |
|---|---|---|
| A（采用） | 单独的显式操作命令，默认无副作用；只调用 Event 表的确保函数 | 让已接受的运维步骤可重复、可审计，同时不扩大 Phase 1 provisioning |
| B | 继续只公开 Python 函数，不提供命令入口 | 保留状态 quo，但生产操作容易变成临时脚本，难以复核调用边界 |
| C | 把 Event 表追加到 Phase 1 表清单或其命令 | 拒绝：违反 ADR-0056 的 Phase 边界并改变冻结的 15 表集合 |

## 后果

- 正面：操作人员可以按明确命令单独创建 / 校验 `event.events`；Phase 1 表注册和自动流程不变。
- 负面 / 代价：新增运维入口、参数校验和隔离测试，需要维护。
- 需要迁移的内容：无。
- 对复现性的影响：无；表结构与批次语义仍由 ADR-0056 / 实现定义。
- 数据库状态：真实 catalog 中的 Event 表仍未创建；本 ADR 只批准入口实现，不代表已执行建表。

## 验证要求

1. 默认运行不会读取 `Settings`、打开 catalog 或调用创建函数。
2. `--apply` 使用显式组合 registry，且只调用 `ensure_event_tables`；验证 Phase 1 的 15 张定义对象及哈希保持不变。
3. 成功输出只包含 Event 表状态，不包含连接设置；Catalog 错误以安全、非零退出方式报告。
4. 测试只使用 mock / 临时 fixture，不连接真实 PostgreSQL 或生产 warehouse。

## 参考

- [ADR-0056](0056-event-table.md)：Event 表语义、显式建表函数与 Phase 1 隔离。
- [ADR-0057](0057-event-request-subject.md)：`EventRequest.subject`。
- [Phase 3 路线图](../research/roadmap.md)：Phase 3 Event & Interaction Engine。
