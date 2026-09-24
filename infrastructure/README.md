# infrastructure/

部署与运行时配置：容器、Compose、可观测性、Adapter 的基础设施侧配置（08-deployment.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `settings.py` | 类型化运行时设置（03-data.md §6.2）：`file://` warehouse / staging、PostgreSQL catalog DSN、HTTP / Binance base URL |
| `storage/` | Phase 1 C1 本地 `file://` `StorageAdapter`（`LocalFileStorageAdapter`） |
| `catalog/` | Phase 1 C2 PostgreSQL-backed PyIceberg `CatalogAdapter[pyarrow.Table]`（`PyIcebergCatalogAdapter`）与定义登记表 |

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
- `TableDefinitionRegistry`：`RegisteredTableDefinition` 把 `(definition_id, version)` 绑定到 PyIceberg Schema、partition spec、表属性与版本化 batch 指纹规则；`definition_hash` 由这些内容的规范 JSON **派生**。C2 只有测试定义；八张生产表与生产指纹规则属 C3。
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

Collector / 下载实现尚未开放（见 `PROJECT_STATUS.md`）。
