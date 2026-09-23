# 00 — Architecture Overview

| 字段 | 值 |
|---|---|
| 状态 | Draft（Architecture Bootstrap） |
| 版本 | 0.1.0 |
| 适用范围 | 全系统 |

## 1. 定位

HLENS-AutoResearch 是**研究基础设施**，其产品是"经过验证的研究结论"，而不是交易信号本身。交易/执行能力是研究结论被晋升后的**下游消费者**。

## 2. 架构原则（冻结）

| # | 原则 | 含义 / 执行方式 |
|---|---|---|
| P1 | Freeze Contracts, Evolve Implementations | 接口、Schema、状态机冻结；实现可随时替换 |
| P2 | Domain Model 优先 | 先定义领域对象与契约，再选择技术 |
| P3 | Research Plane ⟂ Application Plane | 两个平面通过 Promotion 与只读契约交互（01-system.md） |
| P4 | 全面版本化 | Data / Experiment / Strategy / Feature / State / Risk / Outcome 均有版本与内容哈希 |
| P5 | LLM = Provider | Domain 不认识任何具体模型 |
| P6 | Backtest Engine = Provider | Domain 不认识任何具体回测引擎 |
| P7 | 插件化 | Strategy / Feature / State / Risk / Outcome 等通过 Plugin 接入（05-plugin.md） |
| P8 | PostgreSQL ≠ 行情仓库 | PG 只存控制面元数据 |
| P9 | DuckDB ≠ Source of Truth | DuckDB 是计算引擎；真相在 Iceberg/Parquet |
| P10 | 研究数据 = Parquet / Iceberg | 列式、可快照、可时间旅行 |
| P11 | API = OpenAPI / JSON Schema | 契约可机器校验 |
| P12 | 实验可复现 | 06-experiment.md 定义复现元组 |
| P13 | 重大变更 → ADR | docs/adr/ |
| P14 | Constitution 不可为结果让路 | 07-validation.md、research/constitution.md |
| P15 | 失败必须保存 | Failure Registry |
| P16 | 研究代码 ⟂ 生产代码 | Promotion 需重新审查实现 |
| P17 | 技术可替换，契约不可破坏 | 10-migration.md |

## 3. 文档地图

| 文档 | 内容 |
|---|---|
| [01-system.md](01-system.md) | 系统上下文、Plane 划分、System Overview 图、软件分层 |
| [02-domain.md](02-domain.md) | 领域模型、标识与版本化、契约规则 |
| [03-data.md](03-data.md) | 数据分层、存储、时间语义、数据架构图 |
| [04-research-loop.md](04-research-loop.md) | 研究闭环、知识检索、假设生成 |
| [05-plugin.md](05-plugin.md) | Provider 接口、插件清单、注册与隔离 |
| [06-experiment.md](06-experiment.md) | 实验规格、复现元组、运行生命周期 |
| [07-validation.md](07-validation.md) | 验证流水线、晋升状态机、Failure Registry |
| [08-deployment.md](08-deployment.md) | 运行时、分阶段部署、可观测性 |
| [09-security.md](09-security.md) | 密钥、权限、LLM 风险、审计 |
| [10-migration.md](10-migration.md) | 技术替换与 Schema 演化策略 |

## 4. Mermaid 图索引

| # | 图 | 位置 |
|---|---|---|
| D1 | System Overview | [01-system.md §2](01-system.md) |
| D2 | Research Closed Loop | [04-research-loop.md §2](04-research-loop.md) |
| D3 | Software Architecture（分层） | [01-system.md §4](01-system.md) |
| D4 | Data Architecture | [03-data.md §2](03-data.md) |
| D5 | Plugin Architecture | [05-plugin.md §2](05-plugin.md) |
| D6 | Experiment / Validation Lifecycle | [07-validation.md §3](07-validation.md) |
| D7 | Plane 边界与 Promotion | [01-system.md §3](01-system.md) |
| D8 | Experiment Run 状态机 | [06-experiment.md §5](06-experiment.md) |
| D9 | Validation Pipeline | [07-validation.md §2](07-validation.md) |
| D10 | Roadmap 依赖图 | [research/roadmap.md](../research/roadmap.md) |
| D11 | 部署阶段演进 | [08-deployment.md §2](08-deployment.md) |

## 5. 冻结清单 vs 可替换清单

### 冻结（修改需 ADR + 用户批准）

- 架构原则 P1–P17
- Plane 划分与依赖方向（01-system.md）
- Domain 实体与标识/版本化规则（02-domain.md）
- 数据分层与时间语义（03-data.md §3–4）
- Provider 接口的**语义**（05-plugin.md §3）
- 实验复现元组（06-experiment.md §2，含 Validation Profile 版本）
- 三层验证架构：Validation Profile 契约与选择规则（07-validation.md §5，ADR-0007）
- Lifecycle 状态机（07-validation.md §3）
- Validation Constitution 的**原则**（research/constitution.md）

### 可替换（通过 Provider / Infrastructure Adapter）

- Validation Profile 的**参数值**（版本化；Phase 4 校准后冻结，之后只能发布新版本）
- 具体 LLM、回测引擎、计算引擎（DuckDB / Polars）、Iceberg Catalog 实现
- 对象存储实现（本地 FS / MinIO / S3 / R2）
- 消息总线实现（NATS JetStream 为默认）
- 前端框架、图表库、可观测性后端
- 容器编排（Docker Compose → Kubernetes）

## 6. 已知待决事项

见 [docs/adr/README.md §待决事项](../adr/README.md)。
