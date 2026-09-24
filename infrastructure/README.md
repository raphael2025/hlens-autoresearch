# infrastructure/

部署与运行时配置：容器、Compose、可观测性、Adapter 的基础设施侧配置（08-deployment.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `settings.py` | 类型化运行时设置（03-data.md §6.2）：`file://` warehouse / staging、PostgreSQL catalog DSN、HTTP / Binance base URL |
| `storage/` | Phase 1 C1 本地 `file://` `StorageAdapter`（`LocalFileStorageAdapter`） |

## LocalFileStorageAdapter（C1）

- 只接受 settings（或等价的本地绝对 `file://` URI）给出的 warehouse / staging 根；两者必须在同一文件系统。
- 调用方只传 B3 逻辑 object key；实现在每个入口复核 key，并对最终路径做 join / resolve / 根包含检查（拒绝 `..`、symlink 逃逸、映射进 staging 私有区）。
- `stage`：有界流式写入 staging，边写边算 SHA-256 与字节数；校验失败清理**本次**临时文件，不产生可见对象。
- `publish`：同文件系统 `os.link` 原子打通可见性（已存在则不覆盖；同内容幂等，异内容 `ObjectConflict`）；成功后 `fsync` 文件与父目录。
- 无任意 delete / overwrite；不属于当前操作的 orphan 不在写入路径清理。
- 测试只用 pytest `tmp_path`；不访问 `/mnt/*`、网络或数据库。

Catalog / Collector / Iceberg / 下载实现尚未开放（见 `PROJECT_STATUS.md`）。
