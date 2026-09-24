# infrastructure/

部署与运行时配置：容器、Compose、可观测性、Adapter 的基础设施侧配置（08-deployment.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `settings.py` | 类型化运行时设置（03-data.md §6.2）：`file://` warehouse / staging、PostgreSQL catalog DSN、HTTP / Binance base URL |
| `storage/` | Phase 1 C1 本地 `file://` `StorageAdapter`（`LocalFileStorageAdapter`） |

## LocalFileStorageAdapter（C1 / C1-R1 / C1-R2 / C1-R3）

- 只接受 settings（或等价的本地绝对 `file://` URI）给出的 warehouse / staging 根；两者必须在同一文件系统，且不得为同一路径（允许 staging 为 warehouse 的严格子目录或同文件系统上的独立目录）。
- 构造时记录 warehouse / staging 根 inode；**每次操作**临时打开根 FD 并核对仍绑定构造时的 inode，再以 `dir_fd` + `O_NOFOLLOW` 逐段访问，拒绝路径组件或最终对象上的 symlink。实例不跨操作持有根 FD；提供幂等 `close()` / context manager，关闭后操作失败。
- 调用方只传 B3 逻辑 object key；实现在每个入口复核 key，并拒绝映射进 staging 私有区的 key。
- `stage`：有界流式写入 staging；可靠 write-all（短写 / `InterruptedError` / 返回 0）；边写边算 SHA-256 与字节数；校验或写故障清理**本次**临时文件，不产生可见对象。
- `publish`：以已打开的 staging entry FD 与最终父目录 FD 为锚点做同文件系统 `os.link`（不覆盖；同内容幂等，异内容 `ObjectConflict`）；link 后从**实际最终对象 FD**重验普通文件 / SHA-256 / size；再从配置的 warehouse **路径**重新打开根并与构造 inode 比较（不得用操作开始时的旧 FD 代替），用该新根 FD 安全重走 key，确认与本次 final 为同一 inode；失败则用父目录 FD 删除**本次**新建 final。成功返回的 `ObjectRef.uri` 必须立即可 `lookup` / `open_read`（含重启后的 Adapter）。
- `open_read` / `lookup`：对同一个已打开 FD 计算 SHA-256 / size（`open_read` 校验后 `seek(0)` 再返回只读 handle）。
- 无任意 delete / overwrite；不属于当前操作的 orphan 不在写入路径清理。
- 测试只用 pytest `tmp_path`；不访问 `/mnt/*`、网络或数据库。

Catalog / Collector / Iceberg / 下载实现尚未开放（见 `PROJECT_STATUS.md`）。
