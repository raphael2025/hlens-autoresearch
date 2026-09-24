# ADR-0021: Phase 1 本地数据基础设施（D-01、D-02、D-10）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24，批次 A1 起草；A1r 同步 Codex 事实复核；待 Codex 再复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（首次使用）；影响至 Phase 11 |
| 影响范围 | Infrastructure / Data / Plugin |
| 是否破坏兼容 | 否：不改任何现有契约字段；新增 Adapter 契约为增量 |
| 前置 | [ADR-0002](0002-architecture-baseline.md)（技术引入时机需单独 ADR）、[ADR-0017](0017-provider-delivery-schedule.md)（接口先于实现） |
| 关联 | [ADR-0023](0023-bitemporal-revision-data.md)（写入与修订语义） |

## 背景

roadmap Phase 1 要求 Canonical 数据"有快照 ID 且可时间旅行"，禁止"把行情写入 PostgreSQL"和"以 DuckDB 文件作为唯一存储"。
`docs/adr/README.md` 登记了三个在 Phase 1 前必须决定的问题：

- **D-01**：Iceberg 必须有 Catalog；用 PostgreSQL 作 SQL Catalog 可能模糊 Control Plane 与 Data Plane 的边界。
- **D-02**：S3 兼容对象存储通常以容器运行，但 Docker 未安装。
- **D-10**：NATS 的引入时机；早期单机研究不需要事件总线。

ADR-0002 规定默认技术栈中每一项的**引入时机**需要单独 ADR；本 ADR 是 PyIceberg、PostgreSQL SQL Catalog、
本地文件 warehouse 与 EventBus 延期的引入决定。

本机事实：Claude 起草时只读确认 `psql (PostgreSQL) 18.6` 客户端与 `/etc/postgresql/18` 存在；Codex 于 2026-09-24 只读确认服务可用——
`pg_isready` 返回 `/var/run/postgresql:5432 - accepting connections`，`pg_lsclusters` 显示 PostgreSQL 18 main / 5432 online。
**尚未连接任何数据库，未创建任何库或角色。**
`data/*` 已被 `.gitignore` 排除。Docker 未安装（PROJECT_STATUS §7）。

PyIceberg 的能力陈述由 Codex 于 2026-09-24 按官方文档复核（https://py.iceberg.apache.org/configuration/、
https://py.iceberg.apache.org/reference/pyiceberg/table/）：SQL Catalog 支持 PostgreSQL 与 SQLite，SQLite 不适合并发生产用途；
partitioned 表不支持 streaming `RecordBatchReader`。Claude 未联网、未安装依赖；实施批次仍须按锁定的依赖版本做行为 smoke / integration 验证。

## 裁决

### D-01 Iceberg Catalog：PostgreSQL-backed PyIceberg SQL Catalog，与 Control Plane 物理隔离

1. Catalog 使用 PyIceberg 的 SQL Catalog，后端为本机 PostgreSQL。
2. **物理隔离**：Catalog 使用独立 database（建议名 `hlens_iceberg_catalog`）、独立 role、独立凭据；
   未来 Control Plane 使用另一个 database（建议名 `hlens_control`）。禁止跨库 join；Control Plane 不得直接读写
   Iceberg catalog 的内部表；Catalog 库中只有 Iceberg 元数据指针，**不存任何行情数据**（CLAUDE.md H8）。
3. **提交权威路径**：Iceberg 的写入与元数据 commit **只**经由 PyIceberg。DuckDB / Polars 只是计算或读取引擎，
   不拥有 catalog，不得绕过 PyIceberg 提交元数据。
4. **边界**：仓库代码只依赖 `CatalogAdapter` 契约与配置；SQL Catalog 的具体类名不得出现在 `core/` 或 application 层。
5. 本地 warehouse 使用 `file://` URI（D-02）。将来迁往 REST Catalog / S3 时通过 Adapter 与显式迁移映射完成，不改领域契约。
6. **测试分层**：纯单元测试可以用临时 SQLite catalog；集成测试**必须**使用独立的 PostgreSQL test database，
   验证并发提交、快照与重启恢复。**SQLite 绿灯不得当作 PostgreSQL 集成证据**。

### D-02 Warehouse：WSL ext4 本地文件系统 + StorageAdapter，不装 Docker / MinIO

1. Phase 1 的对象存储是 WSL ext4 上的本地文件系统，通过 `StorageAdapter` / PyIceberg FileIO 访问，URI 为 `file://`。
2. 根目录**必须可配置**；开发默认建议 `${repo}/data/warehouse`（`data/` 已被 Git 排除）。**绝不**放在 `/mnt/c` 或其它跨文件系统路径。
3. 任何持久 URI 都由 `StorageAdapter` / FileIO 生成；业务代码不得拼接绝对路径。
4. 写入顺序：staging → 校验 → 原子提交（由 Iceberg commit 确认）；垃圾回收只能删除 catalog 已确认不再引用的文件。
5. 外部既有数据（如 `~/BTC`）只能只读导入 Raw，不得原地修改（CLAUDE.md H13）。
6. MinIO / S3 延期到出现多进程、跨机器或部署等价性需求时；迁移门必须证明 snapshot、content hash 与 PIT 查询结果不变。

### D-10 事件总线：Phase 1 ~ 6 不运行 NATS

1. Phase 1 ~ 6 **不运行** NATS。Collector 的核心 ingest / normalize / commit API **不依赖**消息总线，可以被直接调用。
2. 首次出现任务编排需求时，先交付 `EventBusAdapter` / TaskQueue 的 Protocol、DTO 与 provider-agnostic contract tests
   （ADR-0017 节奏），再提供**确定性的进程内**实现。
3. 进程内实现必须保留与未来实现相同的语义：消息 envelope、idempotency key、attempt 计数、causation / correlation ID、
   ack / failure。不得用随意的 callback API 制造迁移债。
4. **NATS JetStream 引入门**：至少一个真实的跨进程消费者、需要崩溃后重放 / 持久消费、并有幂等集成测试。
   满足该门时引入（预期 Phase 7 或更晚），**最迟在 Phase 11 之前**完成。
5. NATS 只是基础设施，**不得**成为研究结果或领域状态的 Source of Truth。

## 明确不做

- 不在本 ADR 批次创建任何数据库、角色、凭据、warehouse 目录、配置文件或代码；不安装依赖。
- 不引入 Docker、MinIO、Lakekeeper / Polaris 或任何 Java 服务。
- 不采用"Parquet + 自有快照清单"过渡方案，也不把 DuckDB 文件当作真相。
- 不决定 Canonical 表的具体 Schema、分区方案或 Collector 实现（见 ADR-0022、ADR-0023）。
- 不决定 Control Plane 的实现（`hlens_control` 只是命名隔离约定）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** PyIceberg SQL Catalog on PostgreSQL（独立库）+ 本地 `file://` warehouse | 直接满足 Phase 1 的快照与时间旅行；复用本机 PostgreSQL；不引入容器或 Java 服务 | 依赖 PyIceberg 的 SQL Catalog 行为；单机 | — |
| B REST Catalog（Lakekeeper / Polaris） | 更接近未来分布式部署 | 需要额外服务，多数需容器；Docker 未安装 | 超出单机需要，提前运维负担 |
| C Parquet + 自有快照清单，稍后再上 Iceberg | 最简单 | 自造快照语义，之后必须迁移；违背"真相从第一天起是 Iceberg/Parquet" | 迁移债 |
| D PostgreSQL 与 Control Plane 共用同一 database | 少一个库 | 模糊 P3 / P8 边界，权限与备份耦合 | 违背 Plane 隔离 |
| E 现在装 Docker + MinIO | S3 等价 | 需要环境授权；当前无多进程 / 跨机器需求 | 提前引入 |
| F Phase 1 就运行 NATS | 早期统一消息语义 | 单机无消费者，增加运维与故障面 | 无需求 |

## 契约、Schema 与迁移影响

- 现有 38 个契约模型与 `CONTRACT_SCHEMA_VERSION = 2.0.0` **不变**；本 ADR 不修改任何已发布字段。
- 实施批次须按 ADR-0017 先交付 `CatalogAdapter`、`StorageAdapter`（以及首次需要时的 `EventBusAdapter`）的
  Protocol、DTO（带 `schema_version` 并导出 JSON Schema）与 contract tests；新增模型属增量变化（minor），
  若需要修改任何现有必填字段则是破坏性变化，必须升 major 并另起 ADR（D-25）。
- 新依赖（PyIceberg、PyArrow、PostgreSQL 驱动）的引入须在实施批次通过 `uv.lock` 锁定版本，并记录在复现元组的
  `environment_lock` 中。
- 无既有数据需要迁移。

## 失败与恢复语义

- Iceberg commit 失败：已写入但未被任何 snapshot 引用的数据文件视为 orphan，不得被当作数据；按 batch id 幂等重试
  （细节见 ADR-0023）；orphan 只由显式 maintenance 任务在确认未被引用后清理。
- Catalog 数据库不可用：写入 fail closed，不得降级为写本地清单或 SQLite。
- 进程崩溃：重启后从 catalog 当前 snapshot 恢复；staging 中未提交的批次按幂等键重放。
- 本地磁盘损坏或丢失：本阶段无异地副本（无远程、无 S3），这是明确接受的风险，由后续存储迁移解决。

## 安全边界

- Catalog 凭据只来自环境变量或本地权限 600 的文件，永不入 Git（CLAUDE.md H9、09-security.md §2）。
- Catalog role 只拥有 catalog database 的权限，不得访问 `hlens_control`。
- 创建 PostgreSQL database / role、安装依赖属于环境变更（CLAUDE.md H12）：实施批次执行前必须确认 Raphael 的
  持续授权已覆盖并在批次记录中写明；本 ADR 本身不执行任何环境变更。
- 不涉及任何交易所凭据或网络访问（见 ADR-0022）。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | `CatalogAdapter` / `StorageAdapter` 的 Protocol、DTO、contract tests 先于首个实现提交 | 是（ADR-0017） |
| 2 | `core/` 与 application 层中出现 SQL Catalog 具体类名 | 不出现（架构边界测试） |
| 3 | 集成测试使用独立 PostgreSQL test database，覆盖并发提交、快照、时间旅行、重启恢复 | 通过；SQLite 结果不计入 |
| 4 | Catalog database 中存在行情行数据 | 不存在 |
| 5 | warehouse 根目录在 `/mnt/c` 或未配置 | 拒绝启动 |
| 6 | 业务代码拼接绝对路径写文件 | 不存在；全部经 `StorageAdapter` |
| 7 | commit 失败后重试 | 无重复行、无丢行；orphan 不可见 |
| 8 | Catalog 不可用时写入 | fail closed |
| 9 | Collector 核心 API 在无任何消息总线时可调用 | 是 |
| 10 | 仓库与运行时中存在 NATS 服务依赖 | Phase 1 ~ 6 不存在 |
| 11 | 凭据、warehouse 数据出现在 Git 中 | 不出现 |

## 后果

- 正面：Phase 1 从第一天起拥有真正的 Iceberg 快照与时间旅行；不引入容器；Control Plane 与 Data Plane 边界清楚。
- 负面 / 代价：依赖本机 PostgreSQL 与 PyIceberg SQL Catalog 的行为；单机、无异地副本；
  未来迁往 REST Catalog / S3 需要一次有验收门的迁移。
- 对复现性：实验通过 Iceberg `snapshot_id` 引用数据；复现需要 catalog 与 warehouse 同时可用。

## 开放义务

- 实施前在锁定的 PyIceberg 版本上对 SQL Catalog（PostgreSQL）、`file:` FileIO、并发提交与重启恢复做 smoke / integration 验证（Codex 已按官方文档复核能力陈述）。
- PostgreSQL 服务已由 Codex 只读确认在线；实施前仍须按 H12 记录创建 database / role 的授权，并以最小权限连接验证。
- 本地 warehouse 的备份策略与远程仓库 / 异地存储决定（PROJECT_STATUS §6）。
- NATS 引入门满足时另起 ADR，最迟 Phase 11 前。

## 版本策略

- 本 ADR 不改变契约版本。新增 Adapter 契约按 02-domain.md §3：增量为 minor；修改或删除既有字段为 major + ADR。
- Catalog / Storage 的实现替换（REST Catalog、S3、MinIO）通过新 Adapter 实现 + 迁移验收，不改领域契约。

## 合规检查（Proposed 阶段）

- [x] 不修改任何已接受 ADR 正文、Constitution 或契约
- [x] 不在 PostgreSQL 存储行情（H8）；不提交密钥或数据（H9）
- [x] Domain 层仍无具体技术依赖（H7）
- [ ] Codex 复核并接受 —— 待进行
- [ ] 环境变更授权与依赖锁定 —— 实施批次
