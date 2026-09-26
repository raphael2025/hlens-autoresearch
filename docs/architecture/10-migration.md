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
| 删除 / 重命名 / 类型变化 / 语义变化 | major | ADR + 迁移路径 + 旧版本读取器保留至少一个 major。迁移路径由该 major 的 ADR 选定：可以是迁移脚本，也可以是"只读读取器 + 重新登记"（v1 → v2 的做法，见 §3.1） |

数据表 Schema 演化依赖 Iceberg 原生 schema evolution；Feature 语义变化必须发布新 Feature 版本而不是就地修改。

### 3.1 契约 major 的具体路径（已实施：v1 → v2）

契约 `1.0.0 → 2.0.0` 由 [ADR-0008](../adr/0008-contract-payload-immutability.md) 与
[ADR-0009](../adr/0009-experiment-identity-binding.md) 共同定义，是本项目第一次 major 变更。
这一次 major 的**受控迁移路径**是"只读读取器 + 重新登记"：ADR-0008 / 0009 选择**不**提供在线迁移服务、
数据库迁移或把 v1 载荷改写为 v2 的迁移脚本；旧记录只能被只读地识别与追溯，需要 v2 资格的对象必须
按 v2 路径重新登记并重新验证。"旧版本读取器保留至少一个 major"的具体落点：

| 资产 | 路径 | 性质 |
|---|---|---|
| 当前 Schema | `schemas/*.schema.json`（134 份，`schema_version` 默认 `2.0.0`） | 由 `python -m core.contracts.registry` 导出 |
| v1 Schema 快照 | `schemas/v1/*.schema.json`（35 份，默认 `1.0.0`） | **只读、只增不改**；当前导出只写 `schemas/` 顶层，不会覆盖它 |
| v1 固定载荷 + 旧哈希向量 | `tests/vectors/v1/*.json` | 用 v1 代码（commit `066b22d`）生成，时间固定，不依赖 `now` |
| v1 可执行只读入口 | `core/compat/v1.py` 的 `read_v1()` | 返回 `LegacyV1Record` |

规则：

1. **未知 major 拒绝。** 模型校验只接受当前 major（`2.x`，更高 minor 可读取）；
   v1 只读入口只接受 `1.x`。
2. **不重算、不覆盖。** 旧载荷按 v1 当时的排除表与规范化规则计算旧哈希，
   **不得**用 v2 算法重算后赋值。
3. **读取 ≠ 晋升。** `LegacyV1Record` 不是 `Contract`，不能作为 v2 模型、登记或晋升的输入。
   缺 `dependency_hashes` / `run_id` / 结构化 `profile_selection` 绑定的旧实验若要晋升，
   必须按 v2 路径重新登记并重新验证。
4. **哈希不可比较。** v2 的 `experiment_hash` 覆盖面更宽（含策略 / 风控 / Outcome 引用与
   直接依赖内容绑定），且 `ValidationProfile.status` 已退出哈希载荷。
5. **旧哈希的输入前提**（C1 复审 F2）：`read_v1()` 按调用者提供的、通过顶层 shape gate 的**原始** v1 载荷
   复算旧哈希；它不做递归校验，也**不会**补写遗漏的可选 / 默认字段。因此要复现某条历史记录当年
   模型 dump 的身份，输入必须是当时持久化的**完整规范载荷**（v1 `model_dump(mode="json")` 的原样结果）；
   省略了默认字段的载荷可能通过 gate，却得到不同的旧哈希。
6. 本路径**没有**数据库、没有在线迁移服务、不引入 `jsonschema` 依赖。
   仓库内未发现持久化的实验 / Run / Profile / 报告数据；外部历史数据的存在性
   **证据不足**，因此不宣称迁移路径已在真实数据上验证过。

## 4. 复现性在迁移中的保障

- 旧实验永远可以按其复现元组中记录的版本重跑（或至少可读取其产物）。
- 技术替换后，对一组"金标准实验"重跑，结果差异写入迁移 ADR。

## 5. 旧项目迁移

- 旧项目（包括 `~/BTC` 及其他既有代码）**不在当前阶段处理**。
- 未来若要吸收旧代码/数据：旧策略 → 以 KnowledgeItem 或 StrategySpec 形式重新登记并**重新验证**；旧数据 → 经 Collector 以只读方式导入 Raw Zone。旧的回测结论不被直接信任。
