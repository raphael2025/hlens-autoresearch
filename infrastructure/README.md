# infrastructure/

部署与运行时配置：容器、Compose、可观测性、Adapter 的基础设施侧配置（08-deployment.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `settings.py` | 类型化运行时设置（03-data.md §6.2）：`file://` warehouse / staging、PostgreSQL catalog DSN、HTTP / Binance base URL |
| `storage/` | Phase 1 C1 本地 `file://` `StorageAdapter`（`LocalFileStorageAdapter`） |
| `catalog/` | Phase 1 C2 PostgreSQL-backed PyIceberg `CatalogAdapter[pyarrow.Table]`（`PyIcebergCatalogAdapter`）与定义登记表；C3 八张生产表、batch 指纹规则与 partition-spec 演进 |
| `collector/` | Phase 1 D0 Binance 公共现货日归档下载壳（`BinanceSpotArchiveCollector`） |

## LocalFileStorageAdapter（C1 / C1-R1 / C1-R2 / C1-R3）

- 只接受 settings（或等价的本地绝对 `file://` URI）给出的 warehouse / staging 根；两者必须在同一文件系统，且不得为同一路径（允许 staging 为 warehouse 的严格子目录或同文件系统上的独立目录）。
- 构造时记录 warehouse / staging 根 inode；**每次操作**临时打开根 FD 并核对仍绑定构造时的 inode，再以 `dir_fd` + `O_NOFOLLOW` 逐段访问，拒绝路径组件或最终对象上的 symlink。实例不跨操作持有根 FD；提供幂等 `close()` / context manager，关闭后操作失败。
- 调用方只传 B3 逻辑 object key；实现在每个入口复核 key，并拒绝映射进 staging 私有区的 key。
- `stage`：有界流式写入 staging；可靠 write-all（短写 / `InterruptedError` / 返回 0）；边写边算 SHA-256 与字节数；校验或写故障清理**本次**临时文件，不产生可见对象。
- `publish`：以已打开的 staging entry FD 与最终父目录 FD 为锚点做同文件系统 `os.link`（不覆盖；同内容幂等，异内容 `ObjectConflict`）；link 后从**实际最终对象 FD**重验普通文件 / SHA-256 / size；再从配置的 warehouse **路径**重新打开根并与构造 inode 比较（不得用操作开始时的旧 FD 代替），用该新根 FD 安全重走 key，确认与本次 final 为同一 inode；失败则用父目录 FD 删除**本次**新建 final。成功返回的 `ObjectRef.uri` 必须立即可 `lookup` / `open_read`（含重启后的 Adapter）。
- `open_read` / `lookup`：对同一个已打开 FD 计算 SHA-256 / size（`open_read` 校验后 `seek(0)` 再返回只读 handle）。
- 无任意 delete / overwrite；不属于当前操作的 orphan 不在写入路径清理。
- 测试只用 pytest `tmp_path`；不访问 `/mnt/*`、网络或数据库。

## PyIcebergCatalogAdapter（C2）

- 运行时入口只有 `open_postgres_catalog_adapter(settings, registry)`：`Settings.catalog_uri` 必须是 PostgreSQL DSN，warehouse 为 settings 的 `file://`；无任何降级。连接 / 数据库故障 → `CatalogUnavailable`（不回显 DSN）。测试可把临时 SQLite `SqlCatalog` 显式注入构造函数，但 SQLite 结果不是 PostgreSQL 证据。
- `TableDefinitionRegistry`：`RegisteredTableDefinition` 把 `(definition_id, version)` 绑定到 PyIceberg Schema、partition spec、表属性与版本化 batch 指纹规则；`definition_hash` 由这些内容的规范 JSON **派生**。八张生产表与生产指纹规则见下文 C3。
- 建表时绑定写入 Iceberg 表属性；每次访问都重新从登记表解析并核对 Schema / partition / format version / 自有属性，不符 fail closed（`UnknownTableDefinition` / `CatalogIntegrityError`）。
- batch id / 指纹 / 行数 / 指纹规则写入 Iceberg snapshot summary；重放与幂等从 main 分支 snapshot 祖先链恢复，无 sidecar。
- 每表固定 `commit.retry.num-retries=0`：PyIceberg 0.12 的自动重试会丢弃 `AssertRefSnapshotId` 并 rebase，破坏 `expected_parent_snapshot_id` 语义。冲突由 PyIceberg 需求检查与 SQL catalog 的 `metadata_location` CAS 判定，映射为 `CommitConflict`。
- 已知限制：namespace 不应命名为 `staging`（默认 staging 目录是 `warehouse/staging`）；orphan 文件只由后续显式 maintenance 清理。

### 本机 PostgreSQL 资源（H12 授权，2026-09-24）

- `hlens_iceberg_catalog`（production-like）与 `hlens_iceberg_catalog_test`（集成测试）：各自同名 LOGIN role 拥有，无 superuser / createdb / createrole / replication / bypassrls，`REVOKE ALL … FROM PUBLIC`；不存在 `hlens_control`。
- 由 `uv run python -m infrastructure.catalog.provision_local_postgres {catalog|test}` 创建：拒绝覆盖已有资源；只向 PostgreSQL 发送客户端计算的 SCRAM verifier；DSN 写入 Git 忽略、权限 600 的 `.env.catalog` / `.env.catalog-test`，从不打印。
- PostgreSQL 集成测试（未设置变量时显式 skip，skip 不算证据）：

  ```bash
  set -a; . ./.env.catalog-test; set +a   # 不要 echo 变量
  uv run pytest -q -m postgres tests/infrastructure/catalog
  ```

  每个测试用唯一 PyIceberg `catalog_name` 与 `tmp_path` warehouse，结束时经 PyIceberg 删除自己的表 / namespace；fixture 拒绝非 `*_test` 数据库。

## Phase 1 生产表（C3）

- 唯一入口：`infrastructure.catalog.PHASE1_TABLES` / `PHASE1_REGISTRY`（`phase1_tables.py`）——03-data.md §7.1 的八张表，`definition_id` = 表名，`version = 1.0.0`，初始分区按冻结值（`raw.binance_spot_agg_trades` / `canonical.trades`：identity `symbol` + day(`event_time`)；`raw.binance_spot_klines_1m` / `canonical.bars_1m`：identity `symbol` + day(`interval_start`)；其余不分区）。C2 的 test-only 定义只在 `tests/` 中。
- Schema：字段 ID 在源码中逐个写出并等于 Iceberg 建表时分配的 ID（否则 import 失败）；每列带 Iceberg `doc`；时间一律 `timestamptz`（微秒、UTC），交易所十进制值一律 `decimal(38, 18)`，时长为整数微秒，无浮点。列与契约的映射见 `phase1_tables.py` 模块文档；定义哈希由 C2 的规范定义文档派生，golden 值在 `tests/infrastructure/catalog/test_phase1_tables.py`。任何 Schema / 分区 / 属性 / 规则变化 = 新定义版本。
- 幂等建表：`ensure_phase1_tables(adapter)`；已存在且绑定相同则只核对、不改动；任何漂移 fail closed。操作入口（不打印 DSN）：

  ```bash
  set -a; . ./.env.catalog; set +a   # 不要 echo 变量
  uv run python -m infrastructure.catalog.create_phase1_tables
  ```

### Batch 指纹规则 `hlens.pyarrow-batch-sha256@1.0.0`

- 只接受有界 `pyarrow.Table`；按列逻辑拼接（与 chunk 切分无关）后对规范的**逻辑**编码做 SHA-256：null 位图、定宽值（null 槽位为 0）、字符串长度 + UTF-8 字节、list 长度 + 展平值、struct 子列（合并父级 null）。不使用原始 Arrow IPC 字节（null 槽位与切片外字节未定义）、`repr()`、pickle 或调用方指纹；Schema / 字段 metadata 不参与。
- 只接受生产 Schema 需要的类型：`bool`、`int64`、`timestamp[us, UTC]`、`decimal128`、`large_string`、`large_list`、`struct`；其它类型与非空列中的 null → `BatchRejected`。只在 little-endian 主机运行。
- 稳定性边界：以锁定的 `pyarrow==25.0.1` 的 golden vectors 与独立的逐字节推导测试证明；升级 PyArrow 必须保持 golden vectors 不变，编码或类型集合的任何变化 = 新规则版本（新表定义版本）。

### 显式 partition-spec 演进（infrastructure-only，不在核心 Protocol 中）

- `PyIcebergCatalogAdapter.evolve_partition_spec(source, target)`：`target` 必须以 `evolves_from` 绑定已登记的 `source`，且只允许 partition spec 与由此导致的 version / hash 不同（登记表构造时即校验）；目标定义显式声明 Iceberg 将分配的分区字段 ID 与 spec ID。
- 当前表必须按 `source` 完整核对通过；新 spec 与 `hlens.definition.version` / `hash` 在同一个 PyIceberg transaction 中暂存、提交前与 `target` 比对、一次 metadata commit（重试固定为 0）。失败时表保持 `source`；已在 `target` 时幂等返回。不 drop / recreate，不重写数据文件，旧文件保留旧 spec。演进目标不能直接建表。
- 限制：SQL catalog 的 CAS 只保护 spec 需求，不保护其它属性；提交后重新核对，若并发篡改则 `CatalogIntegrityError`（每表单 writer，ADR-0023 §7）。

### 按天分区写入依赖（D-32 → ADR-0026）

PyIceberg 0.12 写入 `day` / `month` / `year` / `hour` / `bucket` 分区需要其官方 extra `pyiceberg-core`。ADR-0026 把直接依赖定为 `pyiceberg[pyarrow,pyiceberg-core,sql-postgres]`（03-data.md §6.1）：`pyiceberg-core` 不是独立顶层依赖，版本由 PyIceberg 0.12 声明的约束（`>=0.10.1,<0.11.0`）解析，`uv.lock` 固定为 `0.10.1`。升级 PyIceberg 时须同时核对该约束。四张 `identity(symbol) + day(...)` 表的 PostgreSQL 用例全部实际写入，并断言真实的 `(symbol, day)` 分区值、replay 同 snapshot 与重启读取。

## BinanceSpotArchiveCollector（D0）

- 入口：`infrastructure.collector.BinanceSpotArchiveCollector`（可用 `from_settings(settings, storage)` 机械工厂）。
- 只访问构造时注入的 archive base（`HLENS_BINANCE_ARCHIVE_BASE_URL`）；descriptor 固定
  `binance.spot.public-archive@1.0.0`，唯一 source `binance.public.spot.archive@1.0.0`，
  `network_origins` = 该 base 的 HTTPS origin。
- 支持范围：`BTCUSDT` / `ETHUSDT` × `agg_trades` / `klines_1m` × UTC 午夜对齐的整日半开区间；
  其它 symbol / type / 非整日边界 / 未声明 source → `UnsupportedRequest`。
- 每个 symbol/day：先 GET `.CHECKSUM`（404/410 → 显式 `SOURCE_ABSENT` gap，不拉 ZIP），
  校验单行 ASCII sha256sum 后，再流式 GET ZIP，经 `StorageAdapter.stage(expected_sha256=…)` →
  `publish` 原子交付；禁止跟随 redirect；同 key 异内容依赖存储层 `ObjectConflict` fail closed。
- 每个 checksum / ZIP GET 的整次尝试（send → 状态 → 完整读或完整流入 stage）受同一
  `http_max_retries` 预算（总尝试 `1 + retries`）；中途 `ReadError`/timeout 可重试，
  redirect / 普通 4xx / 校验与存储冲突不重试。两类 GET 预算独立，ZIP 重试不重取 checksum。
- 构造时严格校验 archive base：拒绝凭据、query、fragment、危险 path；可带安全 path prefix；不静默改写。
- **诚实边界（本批不做）**：不解析 ZIP / 行字段、不构造 revision、不写 Iceberg / Raw、
  不调用 market-data REST、不补尾、不接 WebSocket、不使用 `HLENS_BINANCE_MARKET_DATA_BASE_URL`。
- 测试一律 `httpx.MockTransport` + 真实 `LocalFileStorageAdapter`（`tmp_path`），不访问公网。
