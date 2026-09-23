# ADR-0002: 架构基线（原则、Plane 划分、默认技术栈）

| 字段 | 值 |
|---|---|
| 状态 | Accepted（原则与边界）/ 默认技术栈为"目标默认值"，具体引入时机由后续 ADR 决定；**第 5 条已被 [ADR-0006](0006-strategy-lifecycle.md) 取代（2026-09-23）** |
| 日期 | 2026-09-21 |
| 决策者 | 项目负责人（Architecture Bootstrap 指令） |
| 相关 Phase | Bootstrap |
| 影响范围 | 全系统 |
| 是否破坏兼容 | 否（初始基线） |

## 背景

项目定位为长期演化的加密市场研究基础设施，需要在实现前冻结契约与边界。

## 决策

1. 采纳架构原则 P1–P17（docs/architecture/00-overview.md §2）。
2. 四平面划分：Data / Research / Control / Application（01-system.md §3）。
3. 分层：Presentation → API → Application → Domain ← Plugin / Infrastructure（01-system.md §4）。
4. 目标默认技术栈：
   - Backend：Python、FastAPI、Pydantic、OpenAPI
   - Frontend：React、TypeScript、Vite、Apache ECharts
   - Control Plane：PostgreSQL
   - Research Data：Apache Iceberg + Parquet on S3 兼容对象存储
   - Compute：DuckDB、Polars、NumPy、SciPy、scikit-learn、Statsmodels
   - Event Bus：NATS JetStream
   - Observability：OpenTelemetry、Prometheus、Grafana
   - Runtime：Docker / OCI；Kubernetes-ready，第一阶段不引入 K8s
   - AI：Provider 抽象，Domain 不绑定任何具体 LLM
5. ~~研究对象 Lifecycle 状态机采用 07-validation.md §3 草案（细节待 D-05）。~~ **已被 ADR-0006（Strategy Lifecycle v2）取代，2026-09-23。**

## 后果

- 契约、平面边界与原则变更需新 ADR。
- 技术栈中每一项的**引入时机**（例如 NATS、Iceberg Catalog、MinIO）需单独 ADR，避免过早引入基础设施。
