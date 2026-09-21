# 10 — Migration & Technology Evolution

## 1. 原则

技术栈可以替换；Domain Contract 不能被破坏。每一次替换都是"新 Adapter 实现同一契约 + 一致性测试 + ADR"。

## 2. 替换路径

| 组件 | 替换方式 | 必须通过 |
|---|---|---|
| LLM Provider | 新 `LLMProvider` 实现 | 结构化输出 Schema 测试；记录格式一致 |
| Backtest Engine | 新 `BacktestProvider` | 与参考实现的基准一致性测试 |
| 计算引擎（DuckDB / Polars） | `ComputeEngineAdapter` | Feature 结果按位 / 容差一致 |
| Iceberg Catalog | `CatalogAdapter` | 快照 ID 可迁移或映射表 |
| 对象存储 | `StorageAdapter` | 路径与校验和一致 |
| 事件总线 | `EventBusAdapter` | 至少一次投递 + 幂等消费测试 |
| 前端 | 基于 OpenAPI 客户端重建 | API 契约不变 |
| 编排 | Compose → K8s | 08-deployment.md §3 约束 |

## 3. Schema 演化

| 变化 | 版本 | 要求 |
|---|---|---|
| 添加可选字段 | minor | 向后兼容 |
| 字段语义澄清（无行为变化） | patch | 文档 |
| 删除 / 重命名 / 类型变化 / 语义变化 | major | ADR + 迁移脚本 + 旧版本读取器保留至少一个 major |

数据表 Schema 演化依赖 Iceberg 原生 schema evolution；Feature 语义变化必须发布新 Feature 版本而不是就地修改。

## 4. 复现性在迁移中的保障

- 旧实验永远可以按其复现元组中记录的版本重跑（或至少可读取其产物）。
- 技术替换后，对一组"金标准实验"重跑，结果差异写入迁移 ADR。

## 5. 旧项目迁移

- 旧项目（包括 `~/BTC` 及其他既有代码）**不在当前阶段处理**。
- 未来若要吸收旧代码/数据：旧策略 → 以 KnowledgeItem 或 StrategySpec 形式重新登记并**重新验证**；旧数据 → 经 Collector 以只读方式导入 Raw Zone。旧的回测结论不被直接信任。
