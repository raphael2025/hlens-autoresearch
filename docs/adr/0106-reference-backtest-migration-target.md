# ADR-0106: P14 迁移目标——参考回测引擎

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-10-02，于 `phase/1` 重新接受；2026-10-01 的分支版接受不构成授权）；关闭 P14-TARGET |
| 日期 | 2026-10-01 起草；2026-10-02 接受 |
| 决策者 | Claude Code（PM），依 Raphael 2026-10-02 主会话 `/goal` 指令（「你拥有全权自主决策权……所有技术路径与架构细节均由你自主裁量决定」） |
| 起草者 | Claude Code（PM），依据 2026-10-01 只读审计 |
| 相关 Phase | Phase 14（Technology Migration） |
| 影响范围 | `plugins/backtest/`（新增引擎）、`infrastructure/migration/`（目标描述与报告）、`tests/golden/`、`tests/infrastructure/migration/`；不改 `core/` |
| 是否破坏兼容 | 否 |

## 背景（Context）

ADR-0047 的迁移框架（golden、diff、rollback evidence、conformance）已实现，但没有具体迁移目标，roadmap Phase 14 的"新 Adapter + 金标准重跑报告 + 契约测试全绿"无法完成。`uv.lock` 中没有 DuckDB / Polars / LLM SDK；BacktestProvider 只有一个实现；EventBus 已有内存与文件两种实现但没有走过迁移流程。

## 决策（Decision）

1. **主目标**：BacktestProvider 第二个独立实现 `ReferenceBacktester`（`plugins/backtest/reference.py`），纯 Python + Decimal，逐 bar 状态机，**不复用** `plugins/backtest/bar.py` 的模拟函数。范围仅 `next_bar_open` 成交模型；carry-over、ExecutionModel、`run_with_risk` 不在迁移范围，遇到时显式拒绝。
2. **等价证明**：
   - 对新引擎跑 `BACKTEST_CHECKS`（`run_conformance`）；
   - golden 实验 `tests/golden/experiments/tsmom_g0_g4.py` 改为可注入 backtester（默认值不变，已提交 golden 哈希不得变化）；
   - golden 对比**容差 0**；唯一排除项为 `backtest.result_hash`（含 provider 身份，必然不同），排除在迁移报告中声明；
   - 扰动反例证明对比可证伪；
   - 回滚证据：切回 `BarBacktester` 重跑，容差 0 一致即 RESTORED。
   - 若容差 0 不可达，**不得放宽**：记录差异、迁移判为未通过，另立 ADR 讨论（H3 / H4）。
3. **搭档演练**：EventBus 内存 → `FileEventBus` 正式走一次迁移流程（conformance + 确定性循环 golden + 回滚证据），golden 只选与持久化无关的可观察量（每轮 `record_hash` 序列、投递序列哈希）。
4. **框架补全**：`infrastructure/migration/target.py` 新增 `MigrationTarget`（id、source / target 身份、容差、`excluded_outputs`、范围声明）与 `MigrationReport`（汇总 conformance、golden diff、rollback evidence，规范 JSON、内容哈希、只写一次落盘）。迁移矩阵只放在 `tests/`（infrastructure 不得 import tests）。
5. 新引擎**不**替换生产默认 `BarBacktester`；它是参考实现与迁移演练对象。Catalog 后端迁移暂缓。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| 只追认 EventBus 迁移 | 成本最低 | 不是一次真实的新实现替换 | 作为搭档演练保留 |
| Catalog PostgreSQL ↔ SQLite 迁移 | 贴近存储层 | 需本地 Postgres 实例、快照 ID 映射不确定 | 暂缓 |
| 引入 DuckDB / Polars | 符合 10-migration 示例 | 新依赖、需环境变更 | 无必要 |

## 后果（Consequences）

- 正面：Phase 14 有了可证伪的真实迁移对象与完整的迁移报告链。
- 负面 / 代价：约 900 行代码与测试；同一团队写的两个实现，等价证明力度有限（在报告中注明）。
- 对复现性的影响：既有 golden 哈希不变。

## 合规检查

- [x] 不改冻结契约
- [x] 不修改 Validation Constitution / Profile / 成本模型（H1–H3）
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变
